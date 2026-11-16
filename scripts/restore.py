#!/usr/bin/env python3
"""
scripts/restore.py  (v2.2)
==========================
Разворачивание моста из архива бэкапа на чистой (или «уставшей») системе.

Архив содержит:
    db/bridge.db              — консистентный снапшот SQLite (роли/аккаунты/инвайты/карты)
    profiles/account_<id>/    — профили Chromium (живые сессии MAX)
    MANIFEST.json             — метаданные (версия схемы, состав, время)

Безопасный сценарий по умолчанию: архив раскрывается в КАТАЛОГ-ВОССТАНОВЛЕНИЕ,
ничего существующего не затирается; затем оператор сам переносит данные
(или запускает с --apply, предварительно остановив контейнер).

    # 1) осмотреть архив
    python3 scripts/restore.py /data/backups/bridge_20261115_040000.tar.gz --inspect

    # 2) восстановить в отдельную папку (dry-run по умолчанию для --apply нет)
    docker compose down
    python3 scripts/restore.py bridge_20261115_040000.tar.gz.enc \
        --password "$BACKUP_PASSWORD" --data-root /srv/new-data
    docker compose up -d

Шифрованные архивы (.tar.gz.enc) требуют --password или BACKUP_PASSWORD в
окружении; пароль передаётся openssl через env (не argv).

ВНИМАНИЕ про профили: восстанавливать профиль на ДРУГОЙ машине/архитектуре
обычно безопасно (Chromium сам мигрирует формат), но если MAX решит, что
сессия украдена/подозрительна — аккаунт попросит переавторизацию; это
штатный путь (/admin → 🔑 Переавторизовать), данные при этом не теряются.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

# Разрешаем запуск и из /app (контейнер), и из корня репозитория
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backup import EXCLUDED_DIRS, decrypt_archive, verify_archive  # noqa: E402

SAFE_MEMBERS = {"member", "dir", "fifo", "hardlink", "symlink"}


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def safe_extract(tf: tarfile.TarFile, dest: Path) -> list[str]:
    """Экстракция с защитой от path traversal (../, абсолютные пути, устройства).

    tarfile < 3.12 не имеет фильтра data_filter — делаем свою проверку.
    Симлинки/хардлики создаём только если они ведут ВНУТРЬ dest.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for member in tf.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            print(f"  ⚠️  пропущен опасный путь: {name}")
            continue
        if not (member.isdir() or member.isfile() or member.issym()
                or member.islnk()):
            print(f"  ⚠️  пропущен спецфайл: {name} ({member.type})")
            continue
        target = dest / name
        if not _is_within(dest, target):
            print(f"  ⚠️  выход за пределы dest: {name}")
            continue
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname) if member.issym() \
                else Path(member.name)
            if not _is_within(dest, link_target):
                print(f"  ⚠️  пропущена ссылка наружу: {name} -> {member.linkname}")
                continue
        tf.extract(member, dest)
        written.append(name)
    return written


def inspect(archive: Path, password: str | None) -> int:
    info = verify_archive(archive, password)
    m = info["manifest"]
    print(f"Архив: {archive.name} ({archive.stat().st_size / 1024 / 1024:.2f} МБ)")
    print(f"  MANIFEST: bridge v{m.get('bridge_version', '?')}, "
          f"схема v{m.get('schema_version', '?')}, создано {m.get('created_iso', '?')}")
    print(f"  Аккаунтов: {m.get('accounts_active', '?')} активных из "
          f"{m.get('accounts_total', '?')}, слушателей: {m.get('listeners', '?')}")
    print(f"  БД в архиве: {'да' if info['db'] else 'НЕТ'}")
    print(f"  Профили: {', '.join(info['profiles']) or '—'}")
    if info.get("cache_leak"):
        print(f"  ⚠️  В архив затесался кеш: {info['cache_leak']}")
    return 0 if info["db"] else 1


def restore(archive: Path, data_root: Path, password: str | None,
            *, apply: bool, clean_existing: bool) -> int:
    if not archive.exists():
        print(f"Архив не найден: {archive}")
        return 2
    info = verify_archive(archive, password)
    if not info["db"]:
        print("⛔ В архиве нет db/bridge.db — восстанавливать нечего.")
        return 2

    enc = archive.name.endswith(".enc")
    tmp_dir = Path(tempfile.mkdtemp(prefix="restore_"))
    tar_path = archive
    try:
        if enc:
            if not password:
                print("⛔ Архив зашифрован — передайте --password (или BACKUP_PASSWORD).")
                return 2
            tar_path = tmp_dir / archive.name[:-4]
            print(f"Расшифровываю {archive.name} ...")
            decrypt_archive(archive, password, tar_path)

        print(f"Проверка архива: {info['manifest'].get('created_iso', '?')}, "
              f"профили: {', '.join(info['profiles']) or '—'}")

        if apply:
            root = data_root
        else:
            root = tmp_dir / "extracted"
            print("(режим просмотра: распаковываю во временную папку; "
                  "--apply — записать в --data-root)")

        with tarfile.open(tar_path, "r:gz") as tf:
            names = safe_extract(tf, root)
        print(f"Распаковано объектов: {len(names)}")

        db_src = root / "db" / "bridge.db"
        prof_src = root / "profiles"
        if apply:
            state_dir = data_root / "state"
            profiles_dir = data_root / "profiles"
            state_dir.mkdir(parents=True, exist_ok=True)
            profiles_dir.mkdir(parents=True, exist_ok=True)
            db_dst = state_dir / "bridge.db"
            if db_dst.exists():
                if clean_existing:
                    db_dst.unlink()
                    for wal in (state_dir / "bridge.db-wal",
                                state_dir / "bridge.db-shm"):
                        wal.unlink(missing_ok=True)
                else:
                    backup = db_dst.with_name(f"bridge.db.pre-restore-{int(__import__('time').time())}")
                    db_dst.rename(backup)
                    print(f"  существующая БД сохранена как {backup.name}")
            shutil.move(str(db_src), str(db_dst))
            print(f"  БД: {db_dst}")

            for entry in sorted(prof_src.iterdir()):
                if not entry.name.startswith("account_"):
                    continue
                dst = profiles_dir / entry.name
                if dst.exists():
                    if clean_existing:
                        shutil.rmtree(dst)
                    else:
                        print(f"  ⚠️  профиль {entry.name} уже есть — пропускаю "
                              "(--clean-existing чтобы перезаписать)")
                        continue
                shutil.move(str(entry), str(dst))
                print(f"  профиль: {dst}")

            # sqlite-журналы в архив не попадают (и не нужны): дамп — самодостаточен
            print("\n✅ Восстановление завершено. Запуск: docker compose up -d")
            if info["manifest"].get("schema_version", 0) < 3:
                print("ℹ️  Архив от старой схемы — store.py домигрирует при старте.")
        else:
            print(f"\nСодержимое (временная папка {root}):")
            for p in sorted(root.rglob("*"))[:40]:
                rel = p.relative_to(root)
                print(f"  {'d ' if p.is_dir() else '- '}{rel}")
            if len(list(root.rglob("*"))) > 40:
                print("  ...")
            print("\nДобавьте --apply, чтобы записать в --data-root.")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python scripts/restore.py",
        description="Восстановление max-tg-bridge из архива бэкапа")
    ap.add_argument("archive", type=Path,
                    help="архив bridge_YYYYMMDD_HHMMSS.tar.gz[.enc]")
    ap.add_argument("--password", default=os.getenv("BACKUP_PASSWORD", ""),
                    help="пароль для .enc (или BACKUP_PASSWORD в окружении)")
    ap.add_argument("--data-root", type=Path, default=Path("/data"),
                    help="куда восстанавливать (по умолчанию /data: state/ + profiles/)")
    ap.add_argument("--apply", action="store_true",
                    help="реально записать в --data-root (иначе только распаковка во временную папку)")
    ap.add_argument("--clean-existing", action="store_true",
                    help="перезаписать существующие БД/профили (без --clean-existing старые сохраняются в .pre-restore-*)")
    ap.add_argument("--inspect", action="store_true",
                    help="только показать содержимое архива")
    args = ap.parse_args()

    password = args.password or None
    if args.inspect:
        return inspect(args.archive, password)
    return restore(args.archive, args.data_root, password,
                   apply=args.apply, clean_existing=args.clean_existing)


if __name__ == "__main__":
    raise SystemExit(main())
