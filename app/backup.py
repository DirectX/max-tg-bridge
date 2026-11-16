"""
app/backup.py  (v2.2)
=====================
Резервное копирование состояния моста: SQLite + профили браузеров.

Что и почему попадает в архив
-----------------------------
1. **База** (`/data/state/bridge.db`) выгружается через `sqlite3.Connection.backup()`
   — консистентный снапшот даже под активной записью (backup API сам вычитывает
   WAL в назначение). Обычный `shutil.copy(bridge.db)` **запрещён**: копирует
   main-файл без `-wal`, последние транзакции теряются, файл иногда побит.
   Дамп дополнительно проверяется `PRAGMA quick_check` до упаковки.

2. **Профили** (`/data/profiles/account_<id>/`) упаковываются в tar.gz с
   исключением регенерируемых мусорных папок Chromium (см. `EXCLUDED_DIRS`):
   живые сессии MAX — это Cookies / Local Storage / IndexedDB / Session
   Storage, и они остаются внутри; кеши статики весят сотни мегабайт и
   восстанавливаются сами. Типичный архив 2–5 аккаунтов — единицы МБ.

3. **Шифрование** (опция, `BACKUP_PASSWORD`): внутри лежат живые сессии MAX,
   поэтому при заданном пароле архив упаковывается через
   `openssl enc -aes-256-cbc -pbkdf2` (без внешних python-зависимостей;
   openssl есть в базовом образе jammy). Пароль передаётся через переменную
   окружения дочернего процесса, а не argv — не светится в `ps`.
   Если openssl недоступен — бэкап делается НЕзашифрованным, и в отчёте
   возвращается предупреждение.

Сценарии запуска
----------------
* суточная фоновая задача `backup_scheduler` (по умолчанию 04:00 — после
  плановых ночных релоадов страниц в 03:00+N*7 мин);
* кнопка «💾 Сделать бэкап сейчас» в /admin и команда /backup;
* ротация: хранятся последние `BACKUP_KEEP` (по умолчанию 7) архивов.

Доставка в Telegram
-------------------
Архив отправляется документом супер-админу через наш же Nginx-прокси
(`TelegramCore.send_file_bytes`). Ограничение Bot API на загрузку — 50 МБ:
архив больше не отправляется (остаётся на диске), в отчёте — путь и причина.

Синхронное ядро (`run_backup_core`) вызывается из `asyncio.to_thread`;
async-обёртка `backup_now` добавляет доставку и уведомления — в рабочем
потоке coroutine'и не создаём вовсе (иначе пришлось бы плясать с
run_coroutine_threadsafe).
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from os import environ as os_environ
from pathlib import Path

from .config import Config
from .utils import get_logger

log = get_logger("backup")

# Папки Chromium, которые НЕ несём в архив: регенерируются при первом запуске
# браузера, а весят сотни мегабайт. Сравнение по имени на любой глубине.
EXCLUDED_DIRS: frozenset[str] = frozenset({
    "Cache", "Cache_Data", "Code Cache", "GPUCache", "DawnCache",
    "ShaderCache", "GrShaderCache", "Crashpad", "Crash Reports",
    "blob_storage", "Service Worker", "component_crx_cache",
    "optimization_guide_model_store", "Segmentation Platform",
    "Download Service", "DownloadMetadata", "Safe Browsing",
    "measurement_data", "relay_data", "GraphiteDawnCache",
})
# Файлы-лок-артефакты живой сессии браузера: бесполезны и вредны при восстановлении.
EXCLUDED_FILES: frozenset[str] = frozenset({
    "LOCK", "LOCKFILE", "SingletonLock", "SingletonSocket", "SingletonCookie",
    "DevToolsActivePort", "chrome_debug.log", "CrashpadMetrics-active.pma",
})

# Telegram Bot API: загрузка файлов ботом — 50 МБ.
TG_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


@dataclass(slots=True)
class BackupResult:
    path: Path | None
    size: int = 0
    encrypted: bool = False
    sent: bool = False
    warning: str = ""
    manifest: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.path is not None

    def human_size(self) -> str:
        return f"{self.size / 1024 / 1024:.2f} МБ"


# =========================================================================
# 1. Консистентный дамп SQLite
# =========================================================================
def dump_sqlite(src_db: Path, dst_db: Path) -> None:
    """Консистентная копия БД через backup API + PRAGMA quick_check.

    Read-транзакция не блокирует писателей надолго (WAL), а backup() сама
    вычитывает консистентный снапшот, включая ещё не чекпоинутый -wal.

    ВАЖНО: соединение открывается НЕ в uri-режиме mode=ro. Чтение WAL-базы
    в read-only требует готового -shm, а после падения контейнера его может
    не быть (SQLITE_CANTOPEN). Обычный open позволяет SQLite восстановить
    WAL на месте, а writes запрещаем прагмой query_only — backup() пишет
    только в назначение, источник она не модифицирует.
    """
    if not src_db.exists():
        raise FileNotFoundError(f"База не найдена: {src_db}")
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    dst_db.unlink(missing_ok=True)
    src = sqlite3.connect(src_db)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(dst_db)
        try:
            with dst:
                src.backup(dst)
            row = dst.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise RuntimeError(f"quick_check дампа не прошёл: {row!r}")
        finally:
            dst.close()
    finally:
        src.close()


# =========================================================================
# 2. Упаковка профилей (tar.gz, без регенерируемого мусора Chromium)
# =========================================================================
def _keep_relpath(rel_parts: tuple[str, ...]) -> bool:
    return not any(part in EXCLUDED_DIRS for part in rel_parts)


def _add_tree(tf: tarfile.TarFile, base: Path, arc_prefix: str) -> int:
    """Рекурсивно кладёт base в архив, пропуская EXCLUDED_DIRS/FILES,
    симлинки и сокеты. Возвращает число добавленных файлов."""
    count = 0
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if not _keep_relpath(rel.parts):
            continue
        if path.is_symlink():
            continue
        if path.is_dir():
            info = tf.gettarinfo(str(path), arcname=f"{arc_prefix}/{rel}")
            if info is None:
                continue
            info.mode = 0o755
            tf.addfile(info)
        elif path.is_file():
            if path.name in EXCLUDED_FILES:
                continue
            tf.add(path, arcname=f"{arc_prefix}/{rel}", recursive=False)
            count += 1
    return count


def pack_profiles(profiles_root: Path, archive: Path, *,
                  include_trash: bool = False,
                  extra_files: dict[str, Path] | None = None,
                  manifest: dict | None = None) -> list[str]:
    """Собирает tar.gz: profiles/account_*/ (без кеширующих папок) +
    переданные файлы (дамп БД) + MANIFEST.json. Возвращает список имён
    аккаунт-папок, реально попавших в архив."""
    accounts: list[str] = []
    with tarfile.open(archive, "w:gz") as tf:
        if manifest:
            data = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))

        for arcname, path in (extra_files or {}).items():
            if path.exists():
                tf.add(path, arcname=arcname, recursive=False)

        if profiles_root.is_dir():
            for entry in sorted(profiles_root.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("account_"):
                    _add_tree(tf, entry, f"profiles/{entry.name}")
                    accounts.append(entry.name)
                elif include_trash and entry.name == ".trash":
                    # удалённые аккаунты (profile_path в .trash/account_N_*):
                    # кладём как profiles/.trash/..., в список аккаунтов не попадаем
                    for sub in sorted(entry.iterdir()):
                        if sub.is_dir():
                            _add_tree(tf, sub, f"profiles/.trash/{sub.name}")
    return accounts


# =========================================================================
# 3. Шифрование через openssl (без python-зависимостей)
# =========================================================================
def _openssl_available() -> bool:
    return shutil.which("openssl") is not None


def encrypt_archive(src: Path, password: str) -> Path:
    """src -> src.enc (aes-256-cbc + pbkdf2). Пароль — через env дочернего
    процесса, не argv."""
    dst = src.with_name(src.name + ".enc")
    env = dict(os_environ, _BACKUP_PW=password)
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
         "-in", str(src), "-out", str(dst), "-pass", "env:_BACKUP_PW"],
        env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"openssl enc не удался: {proc.stderr.strip()[:300]}")
    src.unlink(missing_ok=True)
    return dst


def decrypt_archive(src: Path, password: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os_environ, _BACKUP_PW=password)
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
         "-in", str(src), "-out", str(dst), "-pass", "env:_BACKUP_PW"],
        env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"openssl dec не удался: {proc.stderr.strip()[:300]}")
    return dst


# =========================================================================
# 4. Синхронное ядро (внутри asyncio.to_thread)
# =========================================================================
def _count_roles(store) -> tuple[int, int, int]:
    accounts = store.list_accounts()
    active = sum(1 for a in accounts if a.status == "active")
    listeners = sum(len(store.list_listeners(a.id)) for a in accounts)
    return active, len(accounts), listeners


def _rotate(backups_dir: Path, keep: int) -> list[str]:
    """Оставляет самые свежие keep архивов, возвращает список удалённых имён."""
    if keep <= 0:
        return []
    archives = sorted(_iter_archives(backups_dir),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    doomed = archives[keep:]
    names = []
    for p in doomed:
        names.append(p.name)
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Ротация: не удалил %s: %s", p, exc)
    return names


def _iter_archives(d: Path):
    for p in d.iterdir():
        if p.is_file() and (p.name.endswith(".tar.gz")
                            or p.name.endswith(".tar.gz.enc")):
            yield p


def run_backup_core(cfg: Config, store) -> BackupResult:
    """Создаёт архив (дамп БД + профили [+ шифрование]) и ротирует старые.
    Никаких coroutine и сети — только диск/подпроцессы. Вызывается из потока."""
    started = time.time()
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    warning = ""

    tmp = cfg.tmp_dir / f"backup_{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    db_dump = tmp / "bridge.db"
    plain = tmp / f"bridge_{stamp}.tar.gz"
    try:
        # ---- дамп БД ----------------------------------------------------
        try:
            dump_sqlite(cfg.db_path, db_dump)
        except FileNotFoundError as exc:
            return BackupResult(None, warning=str(exc))

        active, total, listeners = _count_roles(store)
        manifest = {
            "created_at": int(time.time()),
            "created_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tz": os_environ.get("TZ", "UTC"),
            "schema_version": store._version(),
            "bridge_version": "2.2",
            "accounts_total": total,
            "accounts_active": active,
            "listeners": listeners,
            "db": "db/bridge.db",
            "profiles_root": "profiles/",
            "excluded_dirs": sorted(EXCLUDED_DIRS),
            "restore_hint": "scripts/restore.py <archive> [--data-root /data]",
        }

        # ---- упаковка ----------------------------------------------------
        accounts_arced = pack_profiles(
            cfg.profiles_root, plain,
            include_trash=cfg.backup_include_trash,
            extra_files={"db/bridge.db": db_dump},
            manifest=manifest,
        )
        manifest["profiles"] = accounts_arced

        # ---- шифрование (опция) ------------------------------------------
        encrypted = False
        artifact = plain
        if cfg.backup_password:
            if _openssl_available():
                artifact = encrypt_archive(plain, cfg.backup_password)
                encrypted = True
            else:
                warning = ("BACKUP_PASSWORD задан, но openssl не найден — "
                           "архив сохранён БЕЗ шифрования")
                log.warning(warning)

        final = cfg.backups_dir / artifact.name
        shutil.move(str(artifact), str(final))
        size = final.stat().st_size

        removed = _rotate(cfg.backups_dir, cfg.backup_keep)
        if removed:
            log.info("Ротация: удалено %d старых: %s", len(removed), ", ".join(removed))
        manifest["rotated_out"] = removed

        log.info("Бэкап готов: %s (%.2f МБ, enc=%s, %.1f c)",
                 final.name, size / 1024 / 1024, encrypted, time.time() - started)
        return BackupResult(final, size, encrypted, False, warning, manifest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================
# 5. Async-обёртки: доставка и планировщик
# =========================================================================
async def backup_now(cfg: Config, store, tg=None) -> BackupResult:
    """Бэкап + (если дан tg и архив влезает в лимит) доставка супер-админу.

    Тяжёлая часть — в потоке; доставка — уже в event loop.
    """
    res = await asyncio.to_thread(run_backup_core, cfg, store)
    if not res.ok:
        return res

    limit_bytes = min(cfg.backup_send_max_mb * 1024 * 1024, TG_UPLOAD_LIMIT_BYTES)
    if tg is None or not cfg.admin_tg_id:
        return res
    if res.size > limit_bytes:
        res.warning = ((res.warning + "; ") if res.warning else "") + (
            f"архив {res.human_size()} > лимита {limit_bytes // 1024 // 1024} МБ — "
            f"в Telegram не отправлен, лежит на диске: {res.path}")
        log.warning(res.warning)
        return res

    m = res.manifest
    caption = (
        f"💾 Бэкап {m.get('created_iso', '?')}\n"
        f"Размер: {res.human_size()}" + (" (AES-256)" if res.encrypted else "") + "\n"
        f"Аккаунтов: {m.get('accounts_active', 0)} активных из {m.get('accounts_total', 0)}, "
        f"слушателей: {m.get('listeners', 0)}\n"
        f"Профили в архиве: {', '.join(m.get('profiles', [])) or '—'}\n"
        f"Восстановление: scripts/restore.py {res.path.name}"
    )
    try:
        data = await asyncio.to_thread(res.path.read_bytes)
        mid = await tg.send_file_bytes(cfg.admin_tg_id, data,
                                       res.path.name, caption)
        res.sent = mid is not None
    except Exception as exc:  # noqa: BLE001
        res.warning = ((res.warning + "; ") if res.warning else "") + f"доставка в TG: {exc}"
        log.exception("Доставка бэкапа админу упала")
    return res


async def backup_scheduler(cfg: Config, store, tg, stop: asyncio.Event,
                           hour: int | None = None, minute: int = 0) -> None:
    """Суточная задача: бэкап в cfg.backup_hour:00 (по умолчанию 04:00 — после
    плановых ночных релоадов страниц аккаунтов)."""
    hour = cfg.backup_hour if hour is None else hour
    log.info("Бэкап-планировщик: ежедневно %02d:%02d, хранение %d, dir=%s",
             hour, minute, cfg.backup_keep, cfg.backups_dir)
    while not stop.is_set():
        now = time.localtime()
        target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                              hour, minute, 0, 0, 0, -1))
        if target <= time.time():
            target += 86400
        try:
            await asyncio.wait_for(stop.wait(), timeout=target - time.time())
            return
        except asyncio.TimeoutError:
            pass
        try:
            res = await backup_now(cfg, store, tg)
            if res.ok:
                await tg.notify_admin(
                    f"💾 Ночной бэкап: {res.path.name} ({res.human_size()}"
                    + (", AES-256" if res.encrypted else "")
                    + (", отправлен в Telegram" if res.sent else ", только на диске")
                    + (")" if not res.warning else f")\n⚠ {res.warning}"))
        except Exception as exc:  # noqa: BLE001
            log.exception("Ночной бэкап не удался")
            await tg.notify_admin(f"⚠️ Ночной бэкап не удался: {exc}")


# =========================================================================
# 6. Диагностика архивов (для restore.py и /backup)
# =========================================================================
def latest_backup(cfg: Config) -> Path | None:
    files = list(_iter_archives(cfg.backups_dir))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def verify_archive(path: Path, password: str | None = None) -> dict:
    """Читает архив: MANIFEST.json, наличие db/bridge.db, список профилей.
    Для .enc нужен password — иначе ValueError (живые сессии не вслепую)."""
    tar_path = path
    tmp_out: Path | None = None
    if path.name.endswith(".enc"):
        if not password:
            raise ValueError("Архив зашифрован — нужен пароль (BACKUP_PASSWORD)")
        base_name = path.name[:-4] if path.name.endswith(".tar.gz.enc") else path.stem
        tmp_out = path.parent / f".verify_{base_name}"
        decrypt_archive(path, password, tmp_out)
        tar_path = tmp_out
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            names = tf.getnames()
            manifest: dict = {}
            if "MANIFEST.json" in names:
                fobj = tf.extractfile("MANIFEST.json")
                if fobj:
                    manifest = json.loads(fobj.read().decode("utf-8"))
            has_db = "db/bridge.db" in names
            profiles = sorted({n.split("/")[1] for n in names
                               if n.startswith("profiles/account_")})
            cache_leak = [n for n in names
                          if any(part in EXCLUDED_DIRS for part in n.split("/"))]
            return {"manifest": manifest, "db": has_db, "profiles": profiles,
                    "members": len(names), "cache_leak": cache_leak[:5]}
    finally:
        if tmp_out is not None:
            tmp_out.unlink(missing_ok=True)
