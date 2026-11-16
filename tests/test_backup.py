"""
tests/test_backup.py
====================
Бэкап-модуль v2.2 без сети и браузера: консистентность дампа SQLite,
исключение кеширующих папок Chromium из архива, шифрование openssl,
ротация, лимит доставки в Telegram, round-trip restore.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backup as bk
from app.store import Store


@pytest.fixture()
def fake_store(cfg):
    """Store + пара аккаунтов со слушателями для манифеста."""
    st = Store(cfg.db_path)
    a = st.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100,
                          status="active")
    st.add_listener(a.id, 101, "Мама")
    st.add_listener(a.id, 102, "Папа")
    b = st.create_account("Робо", chat_title="Кружок", owner_tg_id=200,
                          status="stopped")
    st.add_listener(b.id, 201)
    st.remember_message(a.id, 100, 8001, "k-1", "Учитель", "текст")
    st.mark_seen(a.id, "k-1")
    yield st
    st.close()


@pytest.fixture()
def profiles(cfg):
    """Фейковые профили: живые артефакты сессии + мусор, который обязан быть исключён."""
    root = cfg.profiles_root
    live = {
        "Default/Cookies": b"cookie-db-bytes",
        "Default/Local Storage/leveldb/MANIFEST-000001": b"ls-bytes",
        "Default/IndexedDB/https_web.max.ru_0.indexeddb.leveldb/LOG": b"idb-log",
        "Default/Session Storage/SNAPSHOT": b"session",
        "Local State": b"local-state",
    }
    junk = {
        "Default/Cache/Cache_Data/index": b"x" * 5000,
        "Default/Code Cache/js/index": b"x" * 5000,
        "Default/GPUCache/data_0": b"x" * 5000,
        "Crashpad/reports/crash.dmp": b"x" * 5000,
        "Default/blob_storage/1234/blob": b"x" * 5000,
        "Default/Service Worker/Database/LOG": b"sw",
        "SingletonLock": b"",
        "SingletonSocket": b"",
        "Default/LOCK": b"",
    }
    for acc in ("account_1", "account_2"):
        for rel, data in {**live, **junk}.items():
            p = root / acc / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    # .trash — не должен попасть в архив по умолчанию
    trash = root / ".trash" / "account_9"
    (trash / "Default").mkdir(parents=True, exist_ok=True)
    (trash / "Default/Cookies").write_bytes(b"deleted-session")
    (trash / "Default/Cache/Cache_Data").mkdir(parents=True, exist_ok=True)
    (trash / "Default/Cache/Cache_Data/junk").write_bytes(b"z" * 4000)
    return SimpleNamespace(root=root, live=live, junk=junk)


# ===========================================================================
# 1. Консистентный дамп SQLite
# ===========================================================================
class TestDumpSqlite:
    def test_dump_is_consistent_and_readable(self, cfg, fake_store):
        dst = cfg.tmp_dir / "dump.db"
        bk.dump_sqlite(cfg.db_path, dst)
        assert dst.exists() and dst.stat().st_size > 0
        c = sqlite3.connect(dst)
        try:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"accounts", "listeners", "invites", "message_map"} <= names
            assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
            assert c.execute("SELECT COUNT(*) FROM listeners").fetchone()[0] == 3
            assert c.execute("SELECT max_preview FROM message_map").fetchone()[0] == "текст"
        finally:
            c.close()

    def test_dump_captures_uncheckpointed_wal(self, cfg, fake_store):
        """Дамп обязан включать данные, ещё не чекпоинутые из -wal в main-файл."""
        # пишем транзакцию и НЕ делаем checkpoint (WAL-режим держит её в -wal)
        extra = Store(cfg.db_path)
        extra.create_account("Только что", chat_title="X", owner_tg_id=777)
        wal = cfg.db_path.with_name(cfg.db_path.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, "ожидали непустой WAL"
        dst = cfg.tmp_dir / "dump_wal.db"
        bk.dump_sqlite(cfg.db_path, dst)
        c = sqlite3.connect(dst)
        try:
            cnt = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            assert cnt == 3, f"данные из WAL не попали в дамп: {cnt}"
        finally:
            c.close()
        extra.close()

    def test_missing_db_raises(self, cfg):
        with pytest.raises(FileNotFoundError):
            bk.dump_sqlite(cfg.db_path / "nope.db", cfg.tmp_dir / "x.db")

    def test_source_is_not_modified(self, cfg, fake_store):
        before = cfg.db_path.read_bytes()
        bk.dump_sqlite(cfg.db_path, cfg.tmp_dir / "dump2.db")
        assert cfg.db_path.read_bytes() == before, "dump_sqlite тронул источник"


# ===========================================================================
# 2. Упаковка профилей: кеши ИСКЛЮЧЕНЫ, сессии — внутри
# ===========================================================================
class TestPackProfiles:
    def test_cache_dirs_excluded_sessions_included(self, cfg, profiles, fake_store):
        archive = cfg.tmp_dir / "pack.tar.gz"
        accounts = bk.pack_profiles(profiles.root, archive,
                                    extra_files={"db/bridge.db": cfg.db_path},
                                    manifest={"hello": "world"})
        assert accounts == ["account_1", "account_2"]
        with tarfile.open(archive, "r:gz") as tf:
            names = set(tf.getnames())

        # 1) живые артефакты сессии — внутри (arcname: profiles/<acc>/...)
        for acc in ("account_1", "account_2"):
            assert f"profiles/{acc}/Default/Cookies" in names
            assert f"profiles/{acc}/Default/Local Storage/leveldb/MANIFEST-000001" in names
            assert f"profiles/{acc}/Default/IndexedDB/https_web.max.ru_0.indexeddb.leveldb/LOG" in names
            assert f"profiles/{acc}/Local State" in names
        # 2) кеширующие папки — СНАРУЖИ (требование задачи)
        leaked = [n for n in names
                  if any(part in bk.EXCLUDED_DIRS for part in n.split("/"))]
        assert leaked == [], f"кеш утёк в архив: {leaked[:5]}"
        # 3) лок-артефакты живой сессии — снаружи
        assert not any(n.endswith(("SingletonLock", "SingletonSocket", "LOCK"))
                       for n in names)
        # 4) .trash — не по умолчанию
        assert not any("account_9" in n for n in names)
        # 5) БД и манифест — на месте
        assert "db/bridge.db" in names and "MANIFEST.json" in names
        import json
        with tarfile.open(archive, "r:gz") as tf:
            man = json.loads(tf.extractfile("MANIFEST.json").read().decode())
        assert man["hello"] == "world"

    def test_include_trash_flag(self, cfg, profiles, fake_store):
        """include_trash=True дописывает профили удалённых аккаунтов в .trash/,
        но список «живых» аккаунтов не меняет."""
        archive = cfg.tmp_dir / "pack_trash.tar.gz"
        accounts = bk.pack_profiles(profiles.root, archive, include_trash=True,
                                    manifest=None)
        assert accounts == ["account_1", "account_2"]
        with tarfile.open(archive, "r:gz") as tf:
            names = set(tf.getnames())
        assert "profiles/.trash/account_9/Default/Cookies" in names

    def test_archive_is_small(self, cfg, profiles, fake_store):
        """С 25 МБ «кеширующих» файлов архив обязан весить единицы КБ."""
        archive = cfg.tmp_dir / "small.tar.gz"
        bk.pack_profiles(profiles.root, archive, manifest={"x": 1})
        size = archive.stat().st_size
        junk_total = sum(len(d) for d in profiles.junk.values()) * 2
        assert size < junk_total / 10, f"архив {size} Б не сжал мусор {junk_total} Б"

    def test_symlinks_not_followed(self, cfg, profiles, fake_store):
        secret = cfg.tmp_dir / "outside-secret.txt"
        secret.write_text("do-not-leak")
        link = profiles.root / "account_1" / "Default" / "escape.link"
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip("симлинки недоступны в этой ФС")
        archive = cfg.tmp_dir / "sym.tar.gz"
        bk.pack_profiles(profiles.root, archive, manifest=None)
        with tarfile.open(archive, "r:gz") as tf:
            names = set(tf.getnames())
        assert not any("escape.link" in n for n in names)
        assert "outside-secret.txt" not in names


# ===========================================================================
# 3. Шифрование openssl
# ===========================================================================
class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self, cfg):
        if not bk._openssl_available():
            pytest.skip("openssl не установлен")
        src = cfg.tmp_dir / "plain.bin"
        payload = b"secret session bytes" * 100
        src.write_bytes(payload)
        enc = bk.encrypt_archive(src, "пароль-123")
        assert enc.exists() and enc.name == "plain.bin.enc"
        assert not src.exists(), "исходный plain-файл обязан быть удалён"
        assert enc.read_bytes() != payload
        out = cfg.tmp_dir / "decrypted.bin"
        bk.decrypt_archive(enc, "пароль-123", out)
        assert out.read_bytes() == payload

    def test_wrong_password_fails(self, cfg):
        if not bk._openssl_available():
            pytest.skip("openssl не установлен")
        src = cfg.tmp_dir / "p2.bin"
        src.write_bytes(b"x" * 1000)
        enc = bk.encrypt_archive(src, "right")
        out = cfg.tmp_dir / "o2.bin"
        with pytest.raises(RuntimeError):
            bk.decrypt_archive(enc, "wrong-password", out)
        assert not out.exists()


# ===========================================================================
# 4. Оркестрация: run_backup_core / backup_now
# ===========================================================================
class TestBackupFlow:
    def test_core_creates_archive_and_manifest(self, cfg, fake_store, profiles):
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        assert res.ok and res.path.exists()
        assert res.path.parent == cfg.backups_dir
        assert res.size == res.path.stat().st_size
        m = res.manifest
        assert m["schema_version"] == 3 and m["accounts_active"] == 1
        assert m["accounts_total"] == 2 and m["listeners"] == 3
        assert m["profiles"] == ["account_1", "account_2"]
        info = bk.verify_archive(res.path)
        assert info["db"] is True
        assert info["profiles"] == ["account_1", "account_2"]
        assert info["cache_leak"] == []

    def test_encrypted_when_password_set(self, cfg, fake_store, profiles, monkeypatch):
        if not bk._openssl_available():
            pytest.skip("openssl не установлен")
        monkeypatch.setattr(cfg, "backup_password", "секрет")
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        assert res.ok and res.encrypted
        assert res.path.name.endswith(".tar.gz.enc")
        with pytest.raises(ValueError):
            bk.verify_archive(res.path)              # без пароля — отказ
        info = bk.verify_archive(res.path, "секрет")  # с паролем — читается
        assert info["db"] and info["profiles"] == ["account_1", "account_2"]

    def test_rotation_keeps_last_n(self, cfg, fake_store, profiles):
        import os
        cfg.backups_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            p = cfg.backups_dir / f"bridge_2020010{i}_000000.tar.gz"
            p.write_bytes(b"old" * 10)
            t = time.time() - (10 - i) * 100   # mtime строго по возрастанию
            os.utime(p, (t, t))
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        archives = sorted(cfg.backups_dir.glob("bridge_*"),
                          key=lambda p: p.stat().st_mtime)
        assert len(archives) == cfg.backup_keep, [p.name for p in archives]
        assert archives[-1].name == res.path.name, "свежий архив обязан остаться"
        assert res.manifest["rotated_out"], "старые копии не были удалены"

    def test_delivery_respects_50mb_limit(self, cfg, fake_store, profiles, monkeypatch):
        """Архив больше лимита — на диске остаётся, в Telegram не отправляется."""
        rec = SimpleNamespace(files=[], async_return=None)

        async def send_file_bytes(chat_id, data, filename, caption=""):
            rec.files.append((chat_id, len(data), filename, caption))
            return 42

        tg = SimpleNamespace(send_file_bytes=send_file_bytes,
                             notify_admin=lambda *a, **k: None)
        # раздуваем профиль «мусором», который НЕ исключается (живые файлы)
        import os as _os
        big = profiles.root / "account_1" / "Default" / "IndexedDB" / "big.bin"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(_os.urandom(3 * 1024 * 1024))   # несжимаемо: gz не спасёт
        monkeypatch.setattr(cfg, "backup_send_max_mb", 1)
        res = asyncio.run(bk.backup_now(cfg, fake_store, tg))
        assert res.ok and not res.sent
        assert "лимита" in res.warning
        assert rec.files == []
        assert res.path.exists(), "архив обязан остаться на диске"

    def test_delivery_sends_document(self, cfg, fake_store, profiles):
        sent = []

        async def send_file_bytes(chat_id, data, filename, caption=""):
            sent.append((chat_id, len(data), filename, caption))
            return 42

        tg = SimpleNamespace(send_file_bytes=send_file_bytes,
                             notify_admin=lambda *a, **k: None)
        res = asyncio.run(bk.backup_now(cfg, fake_store, tg))
        assert res.ok and res.sent
        chat_id, size, name, caption = sent[0]
        assert chat_id == cfg.admin_tg_id
        assert size == res.size and name == res.path.name
        assert "Бэкап" in caption and "restore.py" in caption
        assert "1 активных из 2" in caption and "слушателей: 3" in caption

    def test_missing_db_returns_error_result(self, cfg, fake_store):
        cfg.db_path.unlink(missing_ok=True)
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        assert not res.ok and res.path is None and res.warning

    def test_latest_backup(self, cfg, fake_store, profiles):
        assert bk.latest_backup(cfg) is None
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        assert bk.latest_backup(cfg) == res.path


# ===========================================================================
# 5. Восстановление: round-trip архив -> новая БД
# ===========================================================================
class TestRestoreRoundTrip:
    def test_restored_db_matches_source(self, cfg, fake_store, profiles, sandbox):
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        target = sandbox["root"] / "restored"
        target.mkdir(parents=True)
        with tarfile.open(res.path, "r:gz") as tf:
            tf.extract("db/bridge.db", target)
        dst_db = target / "bridge.db"
        shutil.move(str(target / "db" / "bridge.db"), str(dst_db))

        st = Store(dst_db)
        try:
            accs = {a.name: a for a in st.list_accounts()}
            assert st._version() == 3
            assert set(accs) == {"Школа 5Б", "Робо"}
            assert accs["Школа 5Б"].owner_tg_id == 100
            assert accs["Школа 5Б"].status == "active"
            a1 = accs["Школа 5Б"]
            assert sorted(r["tg_user_id"] for r in st.list_listeners(a1.id)) == [101, 102]
            assert st.lookup_message(8001)["max_preview"] == "текст"
            assert st.mark_seen(a1.id, "k-1") is False, "дедуп не пережил восстановление"
        finally:
            st.close()

    def test_restore_script_inspect(self, cfg, fake_store, profiles, sandbox):
        """scripts/restore.py --inspect читает архив без записи на диск."""
        import subprocess
        import sys
        project_root = Path(__file__).resolve().parents[1]
        res = asyncio.run(bk.backup_now(cfg, fake_store))
        proc = subprocess.run(
            [sys.executable, "scripts/restore.py", str(res.path), "--inspect"],
            cwd=str(project_root), capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out = proc.stdout
        assert "MANIFEST" in out and "bridge_version" not in out.split("Аккаунтов:")[0]
        assert "БД в архиве: да" in out
        assert "account_1" in out and "account_2" in out
