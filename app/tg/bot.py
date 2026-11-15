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
    TelegramRetryAfter,
)
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
    def create(api_base: str, proxy: str = "") -> Any:
        from aiogram.client.session.aiohttp import AiohttpSession

        api = TelegramAPIServer.from_base(api_base)

        class _Session(AiohttpSession):
            def __init__(self) -> None:
                for attempt_kwargs in (
                    {"api": api, "proxy": proxy or None},
                    {"api": api},
                    {"proxy": proxy or None} if proxy else {},
                    {},
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
        session = ProxiedAiohttpSession.create(cfg.tg_api_base, cfg.tg_proxy_url)
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

    # ------------------------------------------------------------- lifecycle
    async def prepare(self) -> None:
        me = await self.bot.get_me()
        self.bot_username = me.username
        self.bot_id = me.id
        log.info("Telegram: авторизовался @%s через %s (admin=%s)",
                 me.username, self.cfg.tg_api_base, self.cfg.admin_tg_id)

    def invite_link(self, token: str) -> str | None:
        """Deep-link «https://t.me/<bot>?start=inv_<token>» или None, если
        у бота нет username (тогда ссылку не собрать — только /start inv_...)."""
        if not self.bot_username:
            return None
        return f"https://t.me/{self.bot_username}?start=inv_{token}"

    async def run_polling(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        try:
            await self.bot.close()
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- sends
    async def send_text(self, chat_id: int, text: str,
                        reply_to: int | None = None) -> Optional[int]:
        """Нарезка и отправка текста. None — пользователь недоступен (заблокировал)."""
        last_id: Optional[int] = None
        for chunk in split_text(text):
            try:
                sent = await self._send_with_retry(
                    self.bot.send_message, chat_id=chat_id, text=chunk,
                    reply_to_message_id=reply_to,
                )
                last_id = sent.message_id
            except TelegramForbiddenError:
                log.info("TG %s: пользователь недоступен (заблокировал бота)", chat_id)
                return None
            reply_to = None
        return last_id

    async def send_photo(self, chat_id: int, data: bytes, caption: str = "",
                         filename: str = "photo.png",
                         reply_to: int | None = None) -> Optional[int]:
        caption = truncate(caption or "", MAX_CAPTION)
        try:
            sent = await self._send_with_retry(
                self.bot.send_photo, chat_id=chat_id,
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
                )
                return sent.message_id
            except Exception:  # noqa: BLE001
                log.exception("Фото в Telegram доставить не удалось")
                return None

    async def send_screenshot(self, chat_id: int, path, caption: str = "") -> Optional[int]:
        try:
            sent = await self._send_with_retry(
                self.bot.send_photo, chat_id=chat_id,
                photo=FSInputFile(str(path)), caption=truncate(caption, MAX_CAPTION) or None,
            )
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Скриншот %s отправить не удалось: %s", path, exc)
            return None

    async def send_file_bytes(self, chat_id: int, data: bytes, filename: str,
                              caption: str = "") -> Optional[int]:
        try:
            sent = await self.bot.send_document(
                chat_id, BufferedInputFile(data, filename=filename),
                caption=truncate(caption, MAX_CAPTION) or None,
            )
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Файл %s отправить не удалось: %s", filename, exc)
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
    async def _send_with_retry(self, fn, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return await fn(**kwargs)
            except TelegramRetryAfter as exc:
                wait_s = int(exc.retry_after) + 1
                log.warning("Telegram flood-limit: спим %d c", wait_s)
                await asyncio.sleep(wait_s)
            except (TelegramBadRequest, TelegramForbiddenError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                wait_s = min(2 ** attempt * 2, 30)
                log.warning("Telegram send упал (%s), попытка %d/4, спим %d c",
                            exc, attempt + 1, wait_s)
                await asyncio.sleep(wait_s)
        raise RuntimeError(f"Telegram: 4 неудачные попытки: {last_exc}")

    async def download_bytes(self, file_path: str) -> bytes:
        """Скачивание по file_path через НАШ API-бейс (то есть через прокси)."""
        stream = await self.bot.download_file(file_path)
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
        biggest = message.photo[-1]
        tg_file = await self.bot.get_file(biggest.file_id)
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
