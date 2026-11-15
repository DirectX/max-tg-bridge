"""
app/config.py
=============
Централизованная конфигурация моста MAX <-> Telegram.

ВСЕ секреты и адресные данные читаются ТОЛЬКО из переменных окружения
(в Docker — через .env, локально — `export` или python-dotenv).
Никаких учетных данных в коде и файлах репозитория.

Запуск из корня проекта:
    python -m app.main          # основной режим моста
    python -m app.auth          # интерактивная первичная авторизация MAX
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# python-dotenv опционален: в контейнере переменные приходят из docker --env-file,
# а локально (raw .py, venv) подхватываются из файла .env в корне проекта.
try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Утилиты разбора ENV
# ---------------------------------------------------------------------------

def _req(name: str) -> str:
    """Обязательная переменная. Без неё — падение на старте с понятным текстом."""
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


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Config:
    # ----------------------------- Telegram -------------------------------
    tg_token: str
    tg_chat_id: int
    # Публичный HTTPS-базы собственного реверс-прокси (Nginx) для Telegram Bot API.
    # ВАЖНО: без завершающего слэша, например https://api.example.com.
    # Nginx должен проксировать И /bot{token}/{method}, И /file/bot{token}/{path}
    # на https://api.telegram.org (см. nginx/telegram-api.conf.example).
    tg_api_base: str
    # Опциональный классический forward-прокси для aiohttp (socks5://…, http://…).
    tg_proxy_url: str = ""
    # Разрешённые Telegram user id, denen можно отвечать в MAX (пусто = любой).
    tg_allowed_user_ids: tuple[int, ...] = ()
    # Префикс-режим отправки в MAX без reply: "!макс текст" / "!m текст".
    tg_send_prefixes: tuple[str, ...] = ("!макс", "!m", "/max")
    # Ответы бота в Telegram (ack, ошибки, статусы) помечать preffix-тегом,
    # чтобы человек понимал, что это служебное сообщение моста.
    tg_ack_prefix: str = "🤖"

    # ----------------------------- MAX ------------------------------------
    max_url: str = "https://web.max.ru/"
    # Прямой URL чата (виден в адресной строке при открытом чате, напр.
    # https://web.max.ru/?chatId=… или https://web.max.ru/app/chats/…).
    # Если задан — используется в приоритете; иначе поиск по названию.
    max_chat_url: str = ""
    max_chat_title: str = ""
    # Отображаемое имя ВЛАДЕЛЬЦА аккаунта MAX. Нужно для защиты от эхо-петли:
    # собственные сообщения, возвращающиеся из чата, не пересылаются в TG.
    max_self_name: str = ""
    # Только входящие от этих имён (без учёта регистра, подстрока). Пусто = все.
    max_sender_filter: tuple[str, ...] = ()
    # Скачивать и пересылать изображения из MAX.
    max_download_images: bool = True
    # Максимальный размер изображения, байт (Telegram Bot API лимит — 10 МБ на фото).
    max_image_bytes: int = 9 * 1024 * 1024

    # --------------------------- Прочее / рантайм -------------------------
    profile_dir: Path = field(default_factory=lambda: Path("/data/chrome_profile"))
    tmp_dir: Path = field(default_factory=lambda: Path("/data/tmp"))
    db_path: Path = field(default_factory=lambda: Path("/data/state/bridge.db"))
    # false + запущенный Xvfb (DISPLAY=:99) = «настоящий» видимый браузер:
    # меньше сигналов окружения, проще ручная отладка/капча через VNC.
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    # Час (в локальной таймзоне TZ контейнера) суточной «мягкой» перезагрузки
    # вкладки MAX для профилактики утечек памяти Chromium.
    daily_reload_hour: int = 3
    # Опрос буфера новых сообщений MAX, сек (это не polling сети —
    # чтение памяти страницы, стоимость ~несколько миллисекунд).
    poll_interval: float = 0.7
    # Фоновая проверка «не отвалился ли чат» и переподключения сокета MAX.
    keepalive_interval: float = 90.0
    # Логоточка
    browser_log: Path = field(default_factory=lambda: Path("/data/logs/browser.log"))

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, require_tg: bool = True) -> "Config":
        if require_tg:
            tg_token = _req("TG_BOT_TOKEN")
            tg_chat_id = int(_req("TG_CHAT_ID"))
            tg_api_base = _req("TG_API_BASE").rstrip("/")
        else:
            # Для `python -m app.auth` токены Telegram не нужны.
            tg_token = _opt("TG_BOT_TOKEN", "dummy")
            tg_chat_id = int(_opt("TG_CHAT_ID", "0"))
            tg_api_base = _opt("TG_API_BASE", "https://api.telegram.org").rstrip("/")

        if require_tg and not tg_chat_id:
            raise SystemExit(
                "[config] TG_CHAT_ID пуст. Узнайте ID чата, например у @userinfobot, "
                "и пропишите его в .env (для групп — отрицательное число, напр. -1001234567890)."
            )
        if not tg_api_base.startswith("https://"):
            raise SystemExit(
                f"[config] TG_API_BASE должен быть HTTPS (получено {tg_api_base!r}). "
                "Nginx-прокси Telegram обязан иметь валидный TLS-сертификат."
            )

        cfg = cls(
            tg_token=tg_token,
            tg_chat_id=tg_chat_id,
            tg_api_base=tg_api_base,
            tg_proxy_url=_opt("TG_PROXY_URL"),
            tg_allowed_user_ids=tuple(
                int(x) for x in _csv("TG_ALLOWED_USER_IDS") if x.lstrip("-").isdigit()
            ),
            tg_send_prefixes=tuple(_csv("TG_SEND_PREFIXES", "!макс,!m,/max")) or ("!m",),
            tg_ack_prefix=_opt("TG_ACK_PREFIX", "🤖"),
            max_url=_opt("MAX_URL", "https://web.max.ru/"),
            max_chat_url=_opt("MAX_CHAT_URL"),
            max_chat_title=_opt("MAX_CHAT_TITLE"),
            max_self_name=_opt("MAX_SELF_NAME"),
            max_sender_filter=tuple(x.lower() for x in _csv("MAX_SENDER_FILTER")),
            max_download_images=_bool("MAX_DOWNLOAD_IMAGES", True),
            max_image_bytes=_int("MAX_IMAGE_BYTES", 9 * 1024 * 1024),
            profile_dir=Path(_opt("PROFILE_DIR", "/data/chrome_profile")),
            tmp_dir=Path(_opt("TMP_DIR", "/data/tmp")),
            db_path=Path(_opt("DB_PATH", "/data/state/bridge.db")),
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
            daily_reload_hour=_int("DAILY_RELOAD_HOUR", 3),
            poll_interval=_float("MAX_POLL_INTERVAL", 0.7),
            keepalive_interval=_float("MAX_KEEPALIVE_INTERVAL", 90.0),
            browser_log=Path(_opt("BROWSER_LOG", "/data/logs/browser.log")),
        )
        if not cfg.max_chat_url and not cfg.max_chat_title:
            raise SystemExit(
                "[config] Задайте MAX_CHAT_TITLE (название чата для поиска) "
                "или MAX_CHAT_URL (прямая ссылка на чат)."
            )
        return cfg

    def ensure_dirs(self) -> None:
        for path in (self.profile_dir, self.tmp_dir, self.db_path.parent,
                     self.browser_log.parent):
            path.mkdir(parents=True, exist_ok=True)


def load_config(require_tg: bool = True) -> Config:
    cfg = Config.from_env(require_tg=require_tg)
    cfg.ensure_dirs()
    return cfg
