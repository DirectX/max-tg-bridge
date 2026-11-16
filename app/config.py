"""
app/config.py  (v2, Multi-Account)
==================================
Глобальная конфигурация моста читается из ENV. СВОДНАЯ ПРАВДА О
ПОЛЬЗОВАТЕЛЯХ/АККАУНТАХ — в SQLite (см. store.py); ENV хранит только
инфраструктуру и «настройки по умолчанию» для новых аккаунтов.

Запуск:
    python -m app.main           # боевой режим
    python -m app.auth ...       # аварийный консольный логин (основной — через бота)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"[config] Переменная окружения {name} обязательна. "
            f"Скопируйте .env.example в .env и заполните её."
        )
    return value


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[config] {name} должно быть целым числом, получено: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"[config] {name} должно быть числом, получено: {raw!r}")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


# Telegram Bot API: загрузка файлов ботом — 50 МБ. Не настраивается сверх.
TG_SEND_HARD_LIMIT_MB = 50


@dataclass(slots=True)
class Config:
    # ----------------------------- Telegram (транспорт) -------------------
    tg_token: str
    tg_api_base: str
    tg_proxy_url: str = ""

    # ----------------------------- Ролевая модель -------------------------
    # Единственный владелец /admin и получатель всех системных уведомлений.
    admin_tg_id: int = 0
    # Legacy v1: чат общего моста. Используется ТОЛЬКО сидером миграции v1->v2.
    legacy_tg_chat_id: int = 0

    # ----------------------------- Префиксы/теги --------------------------
    tg_send_prefixes: tuple[str, ...] = ("!макс", "!m", "/max")
    tg_ack_prefix: str = "🤖"

    # ----------------------------- MAX: адреса ----------------------------
    max_url: str = "https://web.max.ru/"
    # Legacy v1 (сидер миграции):
    legacy_max_chat_title: str = ""
    legacy_max_chat_url: str = ""
    legacy_max_self_name: str = ""

    # ----------------------------- MAX: defaults аккаунтов ----------------
    max_download_images: bool = True
    max_image_bytes: int = 9 * 1024 * 1024
    max_sender_filter: tuple[str, ...] = ()
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"

    # ----------------------------- Рантайм --------------------------------
    # Каждый аккаунт живёт в profiles_root/account_<id> — полное разделение
    # cookies / localStorage / IndexedDB между аккаунтами.
    profiles_root: Path = field(default_factory=lambda: Path("/data/profiles"))
    trash_root: Path = field(default_factory=lambda: Path("/data/profiles/.trash"))
    tmp_dir: Path = field(default_factory=lambda: Path("/data/tmp"))
    db_path: Path = field(default_factory=lambda: Path("/data/state/bridge.db"))
    browser_log: Path = field(default_factory=lambda: Path("/data/logs/browser.log"))
    screenshots_dir: Path = field(default_factory=lambda: Path("/data/logs"))

    daily_reload_hour: int = 3            # старт; сдвиг на аккаунт + id*7 минут
    daily_reload_spread_min: int = 7      # шаг раскладки ночных перезагрузок
    poll_interval: float = 0.7
    keepalive_interval: float = 90.0
    # Авторизационная сессия без активности админа — авто-отмена
    auth_idle_timeout: float = 15 * 60.0

    # ------------------- v2.2.1 тайминги Telegram-транспорта ---------------
    # Дефолт aiogram (total=60 c) на медленном/нестабильном ретрансляторе
    # превращал любую задержку в 60 с полного молчания бота, а 4 ретрая —
    # в 4 минуты. Разделяем бюджет по типу операции:
    #   * обычные запросы (sendMessage, getMe, …) — короткий total: они
    #     kilobyte-ные, 60 с ждать нечего, fail-fast + явная ошибка админу;
    #   * медиа (загрузка фото/документа в TG) — длинный: скриншот или бэкап
    #     это мегабайты, и на 30 КБ/с они едут минуты;
    #   * скачивание файлов из TG — тоже длинный бюджет.
    # polling: aiogram сам ставит request_timeout = session.timeout +
    # polling_timeout (см. dispatcher._listen_updates), поэтому long-poll
    # не умирает от короткого session-timeout.
    tg_timeout_total: float = 20.0     # обычные запросы (fail-fast)
    tg_timeout_media: float = 180.0    # send_photo/send_document (upload)
    tg_timeout_download: float = 180.0  # download_file
    tg_send_attempts: int = 3          # попыток на одну отправку в TG
    tg_poll_timeout: int = 10          # long-poll getUpdates, сек
    # Скриншоты страницы MAX: JPEG вместо PNG (1440x900 PNG ~1.5-2 МБ против
    # 120-250 КБ JPEG) — меньше байт, летящих через медленный прокси.
    shot_jpeg: bool = True
    shot_quality: int = 55

    # ----------------------------- v2.2 бэкапы ----------------------------
    backups_dir: Path = field(default_factory=lambda: Path("/data/backups"))
    backup_hour: int = 4                  # суточная копия в 04:00 (после релоадов)
    backup_keep: int = 7                  # локальная ротация: последние 7 копий
    backup_password: str = ""             # пусто = без шифрования; задан = AES-256
    backup_send_max_mb: int = 50          # жёсткий потолок Bot API — 50 МБ
    backup_include_trash: bool = False    # брать в архив profiles/.trash/

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, require_admin: bool = True) -> "Config":
        if require_admin:
            tg_token = _req("TG_BOT_TOKEN")
            tg_api_base = _req("TG_API_BASE").rstrip("/")
            admin_tg_id = int(_req("ADMIN_TG_ID"))
        else:  # аварийный CLI: Telegram не нужен
            tg_token = _opt("TG_BOT_TOKEN", "dummy")
            tg_api_base = _opt("TG_API_BASE", "https://api.telegram.org").rstrip("/")
            admin_tg_id = int(_opt("ADMIN_TG_ID", "0"))

        if admin_tg_id and admin_tg_id >= 10 ** 15:
            raise SystemExit("[config] ADMIN_TG_ID должен быть числовым user id Telegram")
        if not tg_api_base.startswith("https://"):
            raise SystemExit(
                f"[config] TG_API_BASE должен быть HTTPS (получено {tg_api_base!r}). "
                "Nginx-прокси Telegram обязан иметь валидный TLS-сертификат."
            )

        cfg = cls(
            tg_token=tg_token,
            tg_api_base=tg_api_base,
            tg_proxy_url=_opt("TG_PROXY_URL"),
            admin_tg_id=admin_tg_id,
            legacy_tg_chat_id=_int("TG_CHAT_ID", 0),
            tg_send_prefixes=tuple(_csv("TG_SEND_PREFIXES", "!макс,!m,/max")) or ("!m",),
            tg_ack_prefix=_opt("TG_ACK_PREFIX", "🤖"),
            max_url=_opt("MAX_URL", "https://web.max.ru/"),
            legacy_max_chat_title=_opt("MAX_CHAT_TITLE"),
            legacy_max_chat_url=_opt("MAX_CHAT_URL"),
            legacy_max_self_name=_opt("MAX_SELF_NAME"),
            max_download_images=_bool("MAX_DOWNLOAD_IMAGES", True),
            max_image_bytes=_int("MAX_IMAGE_BYTES", 9 * 1024 * 1024),
            max_sender_filter=tuple(x.lower() for x in _csv("MAX_SENDER_FILTER")),
            headless=_bool("MAX_HEADLESS", True),
            user_agent=_opt(
                "MAX_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            ),
            viewport_width=_int("MAX_VIEWPORT_WIDTH", 1440),
            viewport_height=_int("MAX_VIEWPORT_HEIGHT", 900),
            locale=_opt("MAX_LOCALE", "ru-RU"),
            timezone=_opt("MAX_TIMEZONE", "Europe/Moscow"),
            profiles_root=Path(_opt("PROFILES_ROOT", "/data/profiles")),
            trash_root=Path(_opt("TRASH_ROOT", "/data/profiles/.trash")),
            tmp_dir=Path(_opt("TMP_DIR", "/data/tmp")),
            db_path=Path(_opt("DB_PATH", "/data/state/bridge.db")),
            browser_log=Path(_opt("BROWSER_LOG", "/data/logs/browser.log")),
            screenshots_dir=Path(_opt("SCREENSHOTS_DIR", "/data/logs")),
            daily_reload_hour=_int("DAILY_RELOAD_HOUR", 3),
            daily_reload_spread_min=_int("DAILY_RELOAD_SPREAD_MIN", 7),
            poll_interval=_float("MAX_POLL_INTERVAL", 0.7),
            keepalive_interval=_float("MAX_KEEPALIVE_INTERVAL", 90.0),
            auth_idle_timeout=_float("AUTH_IDLE_TIMEOUT", 900.0),
            tg_timeout_total=_float("TG_TIMEOUT_TOTAL", 20.0),
            tg_timeout_media=_float("TG_TIMEOUT_MEDIA", 180.0),
            tg_timeout_download=_float("TG_TIMEOUT_DOWNLOAD", 180.0),
            tg_send_attempts=_int("TG_SEND_ATTEMPTS", 3),
            tg_poll_timeout=_int("TG_POLL_TIMEOUT", 10),
            shot_jpeg=_bool("SHOT_JPEG", True),
            shot_quality=_int("SHOT_QUALITY", 55),
            backups_dir=Path(_opt("BACKUPS_DIR", "/data/backups")),
            backup_hour=_int("BACKUP_HOUR", 4),
            backup_keep=_int("BACKUP_KEEP", 7),
            backup_password=_opt("BACKUP_PASSWORD"),
            backup_send_max_mb=_int("BACKUP_SEND_MAX_MB", 50),
            backup_include_trash=_bool("BACKUP_INCLUDE_TRASH", False),
        )
        # long-poll держит соединение polling_timeout секунд без ответа:
        # session-timeout обязан быть больше, иначе getUpdates будет
        # ложно валиться на каждом цикле (aiogram прибавляет их сам).
        if cfg.tg_timeout_total <= cfg.tg_poll_timeout + 5:
            fixed = cfg.tg_poll_timeout + 10
            print(f"[config] TG_TIMEOUT_TOTAL={cfg.tg_timeout_total} не покрывает "
                  f"long-poll ({cfg.tg_poll_timeout} c) — поднял до {fixed}")
            cfg.tg_timeout_total = fixed
        for name in ("tg_timeout_media", "tg_timeout_download"):
            val = getattr(cfg, name)
            if val < 30:
                print(f"[config] {name.upper()}={val} слишком мало для медиа — 30")
                setattr(cfg, name, 30.0)
        if cfg.tg_send_attempts < 1:
            cfg.tg_send_attempts = 1
        if not 10 <= cfg.shot_quality <= 95:
            print(f"[config] SHOT_QUALITY={cfg.shot_quality} вне разумного диапазона — 55")
            cfg.shot_quality = 55

        if cfg.backup_send_max_mb > TG_SEND_HARD_LIMIT_MB:
            print(f"[config] BACKUP_SEND_MAX_MB={cfg.backup_send_max_mb} урезан до "
                  f"{TG_SEND_HARD_LIMIT_MB} (лимит Bot API на загрузку файлов ботом)")
            cfg.backup_send_max_mb = TG_SEND_HARD_LIMIT_MB
        return cfg

    def ensure_dirs(self) -> None:
        try:
            for path in (self.profiles_root, self.trash_root, self.tmp_dir,
                         self.db_path.parent, self.browser_log.parent,
                         self.backups_dir):
                path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"[config] Не могу создать рабочие каталоги ({exc}). Внутри "
                "контейнера пути создаются Dockerfile'ом; при локальном запуске "
                "переопределите PROFILES_ROOT/TMP_DIR/DB_PATH/... на доступные."
            )

    def account_profile_dir(self, account_id: int) -> Path:
        """Канонический путь изолированного профиля аккаунта."""
        return self.profiles_root / f"account_{account_id}"


def load_config(require_admin: bool = True) -> Config:
    cfg = Config.from_env(require_admin=require_admin)
    cfg.ensure_dirs()
    return cfg
