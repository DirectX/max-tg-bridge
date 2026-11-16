"""
app/tg/bot.py
=============
Telegram-транспорт v2 (aiogram v3).

Сетевая идеология v1 сохранена полностью: БОт ходит ИСКЛЮЧИТЕЛЬНО в
TG_API_BASE — свой HTTPS-реверс Nginx (api.telegram.org заблокирован).
Скачивание файлов из Telegram идёт через тот же бейс (build_file_url).

Ключи v2:
  * проксируемая сессия вынесена в ProxiedAiohttpSession (совместима с
    разными минорными aiogram 3.x: переопределены api=, self.api,
    build_url, build_file_url);
  * все методы отправки принимают ЯВНЫЙ chat_id (личные пользователей,
    админ) — общего чата больше нет;
  * TelegramForbiddenError (пользователь не запустил бота / заблокировал)
    — нормальная часть жизни: API-хелперы возвращают None вместо падения.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.enums import ParseMode
from .format import HTML
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, FSInputFile

try:  # aiogram >= 3.7
    from aiogram.client.default import DefaultBotProperties
except ImportError:  # pragma: no cover
    DefaultBotProperties = None  # type: ignore[assignment, misc]

from ..config import Config
from ..utils import get_logger, split_text, truncate

log = get_logger("tg.core")

MAX_CAPTION = 1024


class ProxiedAiohttpSession:
    """Фабрика AiohttpSession, замотанная на TelegramAPIServer.from_base()."""

    @staticmethod
    def create(api_base: str, proxy: str = "", timeout: float | None = None) -> Any:
        from aiogram.client.session.aiohttp import AiohttpSession

        api = TelegramAPIServer.from_base(api_base)

        class _Session(AiohttpSession):
            def __init__(self) -> None:
                # timeout: бюджет на ОДИН запрос по умолчанию (aiogram ставит
                # DEFAULT_TIMEOUT=60 — на медленном ретрансляторе это 60 с
                # молчания). Медиа-операции переопределяют его per-call.
                base = {"timeout": timeout} if timeout else {}
                for attempt_kwargs in (
                    {"api": api, "proxy": proxy or None, **base},
                    {"api": api, **base},
                    {"proxy": proxy or None, **base} if proxy else base or {},
                    base or {},
                ):
                    try:
                        super().__init__(**attempt_kwargs)
                        break
                    except TypeError:
                        continue
                else:  # pragma: no cover
                    super().__init__()
                self.api = api  # на случай старых версий, читающих self.api

            def build_url(self, bot, *args, **kwargs):  # type: ignore[override]
                method = kwargs.get("method") or (args[0] if args else "")
                return f"{api_base}/bot{bot.token}/{method}"

            def build_file_url(self, bot, *args, **kwargs):  # type: ignore[override]
                path = kwargs.get("path") or kwargs.get("file_path") or (args[0] if args else "")
                return f"{api_base}/file/bot{bot.token}/{path}"

        return _Session()


class TelegramCore:
    """Bot + Dispatcher + транспортные хелперы. Роутеры навешивает main.py."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        session = ProxiedAiohttpSession.create(
            cfg.tg_api_base, cfg.tg_proxy_url, timeout=cfg.tg_timeout_total)
        kwargs: dict[str, Any] = {"session": session}
        if DefaultBotProperties is not None:
            kwargs["default"] = DefaultBotProperties(parse_mode=None)
        else:  # pragma: no cover
            kwargs["parse_mode"] = None
        self.bot = Bot(cfg.tg_token, **kwargs)
        self.dp = Dispatcher(storage=MemoryStorage())
        self._ack_ts: dict[int, float] = {}
        # заполняется в prepare(): нужен для сборки инвайт-deep-link ссылок
        self.bot_username: str | None = None
        self.bot_id: int | None = None
        # v2.2.1: в прокси может быть настроен только /bot (API-вызовы), но не
        # /file (скачивание медиа). Тогда текстовые ответы работают, а фото из
        # Telegram не скачаются. Факт проверяется один раз в prepare().
        self.file_endpoint_available: bool = True

    # ------------------------------------------------------------- lifecycle
    async def prepare(self) -> None:
        me = await self.bot.get_me()
        self.bot_username = me.username
        self.bot_id = me.id
        log.info("Telegram: авторизовался @%s через %s (admin=%s)",
                 me.username, self.cfg.tg_api_base, self.cfg.admin_tg_id)
        await self._probe_endpoints()

    async def _probe_endpoints(self) -> None:
        """Один раз при старте проверяем ОБА эндпоинта прокси.

        /bot  — API-вызовы (sendMessage, getUpdates, getFile);
        /file — скачивание медиа (фото Владельца по пути, полученному от getFile).

        Типовая конфигурация «только API»: реверс-прокси настраивают на путь
        /bot, а /file забывают — тогда getFile отвечает, но download_file по
        file_path уходит в 404/timeout. Мост это переживает: текст работает,
        фото честно пропускаются с предупреждением админу.
        """
        import aiohttp
        base = self.cfg.tg_api_base.rstrip("/")
        token = self.cfg.tg_token
        # ВАЖНО: берём сессию У ЖЕ НАСТРОЕННОГО транспорта, а не новую —
        # иначе при заданном TG_PROXY_URL проба пойдёт мимо прокси и честно
        # работающий /file будет объявлен «не настроен».
        try:
            sess = await self.bot.session.create_session()
        except Exception as exc:  # noqa: BLE001
            log.warning("Проба эндпоинтов пропущена (нет сессии: %s)", exc)
            return
        try:
                # /bot — контрольный getMe (дешёвый, уже проходил)
                async with sess.get(f"{base}/bot{token}/getMe",
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log.error("⛔ Прокси /bot вернул %s — мост работать не будет",
                                  resp.status)
                    else:
                        log.info("✓ Прокси /bot отвечает (%s)", resp.status)

                # /file — GET на заведомо отсутствующем пути. Различаем:
                #   проксируется  -> api.telegram.org: 404 + JSON {"ok":false}
                #   не настроен   -> своя 404/502 от Nginx: text/html
                # Именно GET, а не HEAD: Bot API на HEAD отвечает 405 даже
                # когда маршрут проксируется нормально (иначе получили бы
                # ложное «/file не работает» и зря отключили скачивание фото).
                url = f"{base}/file/bot{token}/probe.test"
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    body = (await resp.text())[:200] if resp.status else ""
                    looks_like_telegram = "json" in ctype and '"ok"' in body
                    if resp.status == 200 or looks_like_telegram:
                        self.file_endpoint_available = True
                        log.info("✓ Прокси /file отвечает (status=%s, ct=%s) — "
                                 "скачивание фото из Telegram доступно",
                                 resp.status, ctype or "?")
                    else:
                        self.file_endpoint_available = False
                        log.warning(
                            "⚠ Эндпоинт /file НЕ проксирует Telegram API "
                            "(status=%s, ct=%r, body=%r): фото из Telegram "
                            "скачиваться не будут. Добавьте в Nginx второй "
                            "location — см. nginx/telegram-api.conf.example и "
                            "README §3.1. ТЕКСТОВЫЙ мост при этом работает.",
                            resp.status, ctype, body[:80])
        except Exception as exc:  # noqa: BLE001
            log.warning("Проба эндпоинтов не удалась (%s) — продолжаем без "
                        "диагноза /file", exc)
        # сессию НЕ закрываем: она принадлежит транспорту (её закроет close())

    def invite_link(self, token: str) -> str | None:
        """Deep-link «https://t.me/<bot>?start=inv_<token>» или None, если
        у бота нет username (тогда ссылку не собрать — только /start inv_...)."""
        if not self.bot_username:
            return None
        return f"https://t.me/{self.bot_username}?start=inv_{token}"

    async def run_polling(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(
            self.bot,
            handle_signals=False,
            polling_timeout=self.cfg.tg_poll_timeout,
        )

    async def close(self) -> None:
        # ВАЖНО: bot.close() — это НЕ освобождение ресурсов, а метод Telegram
        # API «close» (logout): он лениво создаёт aiohttp-сессию, шлёт запрос и
        # при недоступности прокси валится, оставляя сессию открытой ->
        # «Unclosed client session» + SSL-мусор на закрытом loop.
        # Ресурсы освобождает session.close() (сам ждёт 250 мс на SSL-коннекты).
        try:
            await self.bot.session.close()
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- sends
    # ---------------------------------------------------- HTML-варианты (v2.2.1)
    # Панели/статусы/FSM-подсказки ходят ТОЛЬКО через них: parse_mode=HTML
    # передаётся явно, поэтому «забыли тег → пользователь видит <b>» невозможно.
    # Фан-аут контента из MAX остаётся flat (send_text/send_photo без parse_mode):
    # внешний текст может содержать угловые скобки — экранировать его негде и
    # незачем, плоский режим безопаснее.
    async def send_html(self, chat_id: int, text: str,
                        reply_to: int | None = None) -> Optional[int]:
        return await self.send_text(chat_id, text, reply_to, parse_mode=HTML)

    async def send_text(self, chat_id: int, text: str,
                        reply_to: int | None = None,
                        parse_mode: str | None = None) -> Optional[int]:
        """Нарезка и отправка текста. None — пользователь недоступен (заблокировал)."""
        last_id: Optional[int] = None
        for chunk in split_text(text):
            try:
                sent = await self._send_with_retry(
                    self.bot.send_message, chat_id=chat_id, text=chunk,
                    reply_to_message_id=reply_to,
                    **({"parse_mode": parse_mode} if parse_mode else {}),
                )
                last_id = sent.message_id
            except TelegramForbiddenError:
                log.info("TG %s: пользователь недоступен (заблокировал бота)", chat_id)
                return None
            except TelegramBadRequest as exc:
                # битая разметка в служебном сообщении: не роняем отправку,
                # уходим в flat (текст важен больше, чем жирный шрифт)
                log.warning("send_message отклонён (%s) — повторяю без parse_mode", exc)
                sent = await self._send_with_retry(
                    self.bot.send_message, chat_id=chat_id, text=chunk,
                    reply_to_message_id=reply_to,
                )
                last_id = sent.message_id
            reply_to = None
        return last_id

    async def send_photo(self, chat_id: int, data: bytes, caption: str = "",
                         filename: str = "photo.png",
                         reply_to: int | None = None) -> Optional[int]:
        caption = truncate(caption or "", MAX_CAPTION)
        try:
            sent = await self._send_with_retry(
                self.bot.send_photo, _timeout=self.cfg.tg_timeout_media,
                chat_id=chat_id,
                photo=BufferedInputFile(data, filename=filename),
                caption=caption or None, reply_to_message_id=reply_to,
            )
            return sent.message_id
        except TelegramForbiddenError:
            log.info("TG %s: пользователь недоступен, фото пропущено", chat_id)
            return None
        except TelegramBadRequest as exc:
            log.error("send_photo отклонён (%s) — пробую send_document", exc)
            try:
                sent = await self.bot.send_document(
                    chat_id, BufferedInputFile(data, filename=filename),
                    caption=caption or None, reply_to_message_id=reply_to,
                    request_timeout=int(self.cfg.tg_timeout_media),
                )
                return sent.message_id
            except Exception:  # noqa: BLE001
                log.exception("Фото в Telegram доставить не удалось")
                return None

    async def send_screenshot(self, chat_id: int, path, caption: str = "") -> Optional[int]:
        try:
            sent = await self._send_with_retry(
                self.bot.send_photo, _timeout=self.cfg.tg_timeout_media,
                chat_id=chat_id,
                photo=FSInputFile(str(path)), caption=truncate(caption, MAX_CAPTION) or None,
            )
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Скриншот %s отправить не удалось: %s", path, exc)
            return None

    async def send_file_bytes(self, chat_id: int, data: bytes, filename: str,
                              caption: str = "") -> Optional[int]:
        try:
            sent = await self._send_with_retry(
                self.bot.send_document, _timeout=self.cfg.tg_timeout_media,
                chat_id=chat_id,
                document=BufferedInputFile(data, filename=filename),
                caption=truncate(caption, MAX_CAPTION) or None,
            )
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Файл %s отправить не удалось: %s", filename, exc)
            return None

    async def notify_admin_html(self, text: str, reply_markup=None) -> Optional[int]:
        """HTML-уведомление админу (панель/статусы); flat-фолбэк при ошибке."""
        if not self.cfg.admin_tg_id:
            log.warning("ADMIN_TG_ID не задан, уведомление потеряно: %s", text)
            return None
        try:
            sent = await self.bot.send_message(self.cfg.admin_tg_id, text,
                                               parse_mode=HTML, reply_markup=reply_markup)
            return sent.message_id
        except TelegramBadRequest as exc:
            log.warning("HTML-уведомление админу отклонено (%s) — шлю flat", exc)
            return await self.notify_admin(text, reply_markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("Уведомление админу не ушло: %s", exc)
            return None

    async def notify_admin(self, text: str, reply_markup=None) -> Optional[int]:
        if not self.cfg.admin_tg_id:
            log.warning("ADMIN_TG_ID не задан, уведомление потеряно: %s", text)
            return None
        try:
            sent = await self.bot.send_message(
                self.cfg.admin_tg_id, text, reply_markup=reply_markup)
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Уведомление админу не ушло: %s", exc)
            return None

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self.bot.send_chat_action(chat_id, "typing")
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- helpers
    async def _send_with_retry(self, fn, *, _timeout: float | None = None, **kwargs):
        """Отправка с ретраями. _timeout — бюджет ОДНОЙ попытки (для медиа он
        длиннее, чем для текстовых запросов): aiogram принимает request_timeout
        на каждый вызов метода.

        Важно: при таймауте соединения Telegram уже МОГ принять сообщение
        (запрос ушёл, ответ не дождались) — поэтому повтор для текстовых
        уведомлений допустим, а для критичных «отправить один раз» операций
        вызывающий должен это учитывать.
        """
        attempts = max(1, self.cfg.tg_send_attempts)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                if _timeout is not None:
                    return await fn(request_timeout=_timeout, **kwargs)
                return await fn(**kwargs)
            except TelegramRetryAfter as exc:
                wait_s = int(exc.retry_after) + 1
                log.warning("Telegram flood-limit: спим %d c", wait_s)
                await asyncio.sleep(wait_s)
                attempts = max(attempts, attempt + 2)   # лимит не считается провалом
            except (TelegramBadRequest, TelegramForbiddenError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                wait_s = min(2 ** attempt * 2, 20)
                log.warning("Telegram send упал (%s), попытка %d/%d, спим %d c",
                            exc, attempt + 1, attempts, wait_s)
                await asyncio.sleep(wait_s)
        raise TelegramNetworkError(
            method=getattr(fn, "__name__", "?"),
            message=f"нет ответа от {self.cfg.tg_api_base} после {attempts} попыток "
                    f"(бюджет {(_timeout or self.cfg.tg_timeout_total):.0f} c на попытку): "
                    f"{last_exc}",
        )

    async def download_bytes(self, file_path: str) -> bytes:
        """Скачивание по file_path через НАШ API-бейс (то есть через прокси)."""
        stream = await self.bot.download_file(
            file_path, timeout=int(self.cfg.tg_timeout_download))
        buf = io.BytesIO()
        if hasattr(stream, "read"):
            while True:
                piece = (await stream.read(256 * 1024)) \
                    if asyncio.iscoroutinefunction(stream.read) \
                    else stream.read(256 * 1024)
                if not piece:
                    break
                buf.write(piece)
        data = buf.getvalue()
        if hasattr(stream, "close"):
            result = stream.close()
            if asyncio.iscoroutine(result):
                await result
        return data

    async def extract_photo(self, message: Message) -> tuple[bytes, str] | None:
        if not message.photo:
            return None
        if not self.file_endpoint_available:
            # прокси без /file: не пытаемся качать (это 60 с таймаута на ровном
            # месте), честно объясняем владельцу один раз в 10 минут
            await self.throttled_ack(
                message.chat.id,
                "в вашем Telegram-прокси не настроен /file — фото из TG не "
                "скачиваются, текстовые ответы работают. Как добавить: "
                "nginx/telegram-api.conf.example (путь /file).",
                window_s=600)
            return None
        biggest = message.photo[-1]
        tg_file = await self.bot.get_file(
            biggest.file_id, request_timeout=int(self.cfg.tg_timeout_total))
        data = await self.download_bytes(tg_file.file_path)
        if not data:
            return None
        return data, f"tg_{message.message_id}.jpg"

    # -------------------------------------------------- throttled подсказки
    async def throttled_ack(self, chat_id: int, note: str,
                            window_s: float = 300.0, key: int | None = None) -> None:
        """Одна подсказка тому же пользователю не чаще, чем раз в window_s."""
        k = key if key is not None else chat_id
        now = asyncio.get_event_loop().time()
        if now - self._ack_ts.get(k, 0.0) < window_s:
            return
        self._ack_ts[k] = now
        await self.send_text(chat_id, f"{self.cfg.tg_ack_prefix} {note}")
