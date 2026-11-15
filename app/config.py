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
        )
        return cfg

    def ensure_dirs(self) -> None:
        try:
            for path in (self.profiles_root, self.trash_root, self.tmp_dir,
                         self.db_path.parent, self.browser_log.parent):
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
