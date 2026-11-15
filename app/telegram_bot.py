"""
app/telegram_bot.py
===================
Telegram-сторона моста на aiogram v3.

Работа через свой HTTPS-реверс-прокси (Nginx), потому что api.telegram.org
с сервера в РФ заблокирован:

    from aiogram.client.telegram import TelegramAPIServer
    api = TelegramAPIServer.from_base("https://api.example.com")

В разных минорных версиях aiogram 3.x способ прокинуть этот сервер в сессию
незначительно менялся (`api=`, потом `build_url()/build_file_url()`), поэтому
ниже — совместимый подкласс ProxiedAiohttpSession: он переопределяет ВСЕ
существующие точки сборки URL и гарантированно ходит только в TG_API_BASE.
Тот же сценарий покрывает скачивание файлов (bot.download_file) — файл-URL
строится из того же base, то есть картинки из Telegram тоже идут через прокси.

Транспортная логика; правила маршрутизации — в app/bridge.py.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

try:  # aiogram >= 3.7
    from aiogram.client.default import DefaultBotProperties
except ImportError:  # pragma: no cover
    DefaultBotProperties = None  # type: ignore[assignment, misc]

from .config import Config
from .utils import get_logger, split_text, truncate

log = get_logger("telegram")

MAX_CAPTION = 1024
ACK_SPAM_WINDOW = 300.0  # не чаще одной подсказки в 5 минут


class ProxiedAiohttpSession:
    """Фабрика AiohttpSession, замотанная на кастомный TelegramAPIServer.

    Возвращает экземпляр класса-наследника с переопределёнными build_url /
    build_file_url (aiogram 3.x разных минорных версий).
    """

    @staticmethod
    def create(api_base: str, proxy: str = "") -> Any:
        from aiogram.client.session.aiohttp import AiohttpSession

        api = TelegramAPIServer.from_base(api_base)

        class _Session(AiohttpSession):  # создаём класс лениво: нужен импорт сессии
            def __init__(self) -> None:
                kwargs: dict[str, Any] = {}
                # Вариант A: сессия принимает готовый TelegramAPIServer (api=…).
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
                # Вариант B: url-билдеры — гарантированно наши (см. докстринг).
                self.api = api  # на случай старых версий, читающих self.api

            def build_url(self, bot, *args, **kwargs):  # type: ignore[override]
                method = kwargs.get("method") or (args[0] if args else "")
                return f"{api_base}/bot{bot.token}/{method}"

            def build_file_url(self, bot, *args, **kwargs):  # type: ignore[override]
                path = kwargs.get("path") or kwargs.get("file_path") or (args[0] if args else "")
                return f"{api_base}/file/bot{bot.token}/{path}"

        return _Session()


@dataclass(slots=True)
class TgIncoming:
    """Нормализованное входящее сообщение из Telegram, годное для MAX."""

    text: str
    photo_bytes: bytes | None
    photo_name: str
    reply_to_max_preview: str | None
    author: str
    tg_message_id: int


class TelegramBridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

        session = ProxiedAiohttpSession.create(cfg.tg_api_base, cfg.tg_proxy_url)
        kwargs: dict[str, Any] = {"session": session}
        if DefaultBotProperties is not None:
            kwargs["default"] = DefaultBotProperties(parse_mode=None)
        else:  # pragma: no cover
            kwargs["parse_mode"] = None
        self.bot = Bot(cfg.tg_token, **kwargs)

        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)

        # Колбэки устанавливает bridge.py (без циклического импорта).
        self.on_incoming: Callable[[TgIncoming], Awaitable[None]] | None = None
        self.on_command: Callable[[str], Awaitable[str]] | None = None

        self._last_ack: dict[int, float] = {}
        self._register_handlers()

    # ------------------------------------------------------------ доступ
    def _allowed(self, message: Message) -> bool:
        ids = self.cfg.tg_allowed_user_ids
        if not ids:
            return True
        return bool(message.from_user and message.from_user.id in ids)

    def _from_target_chat(self, message: Message) -> bool:
        # ЛС с ботом тоже принимаем: там удобно узнавать chat_id и стартовать.
        return message.chat.id == self.cfg.tg_chat_id or message.chat.type == "private"

    # ------------------------------------------------------------ handlers
    def _register_handlers(self) -> None:
        r = self.router

        @r.message(CommandStart())
        async def cmd_start(message: Message) -> None:
            if not self._allowed(message):
                return
            prefixes = " / ".join(self.cfg.tg_send_prefixes)
            await message.answer(
                f"{self.cfg.tg_ack_prefix} Мост MAX ⇄ Telegram на связи.\n"
                f"ID этого чата: `{message.chat.id}` — пропишите в TG_CHAT_ID.\n\n"
                "Как отвечать в MAX:\n"
                "• Reply (ответ) на сообщение, пересланное из MAX — уйдёт "
                "ответом на то же сообщение (best-effort) или цитатой;\n"
                f"• начать сообщение с «{prefixes}» — уйдёт обычным сообщением;\n"
                "• фото reply-ом или с префиксом — загрузится в MAX картинкой."
            )

        @r.message(Command("id"))
        async def cmd_id(message: Message) -> None:
            uid = message.from_user.id if message.from_user else None
            await message.answer(f"chat_id=`{message.chat.id}` user_id=`{uid}`")

        @r.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not (self._from_target_chat(message) and self._allowed(message)):
                return
            report = await self.on_command("status") if self.on_command else "bridge не готов"
            await message.answer(f"{self.cfg.tg_ack_prefix} {report}")

        # Сообщения от имён/ботов в канале и любые is_bot — игнор.
        @r.message(F.from_user.is_bot)
        async def _ignore_bots(message: Message) -> None:
            return None

        # Всё остальное (текст, фото, reply, альбомы по одному сообщению).
        @r.message()
        async def any_message(message: Message) -> None:
            await self._handle_incoming(message)

    # ----------------------------------------------------------- incoming
    async def _handle_incoming(self, message: Message) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        if not self._from_target_chat(message) or not self._allowed(message):
            return

        text = (message.text or message.caption or "").strip()
        photo = await self._extract_photo(message)
        reply_preview = self._reply_from_max_preview(message)

        is_prefixed = False
        low = text.lower()
        for pref in self.cfg.tg_send_prefixes:
            p = pref.lower()
            if low == p or low.startswith(p + " ") or low.startswith(p + "\n"):
                text = text[len(pref):].strip()
                is_prefixed = True
                break

        if not text and photo is None:
            return  # стикеры/реакции/сервисные — мимо

        if reply_preview is None and not is_prefixed:
            await _throttled_ack(
                self, message.message_id,
                "чтобы отправить в MAX — сделайте reply на сообщение из MAX "
                "или начните с префикса",
            )
            return

        if self.on_incoming is None:
            log.warning("Сообщение из TG при незакрытом колбэке: %r", text[:60])
            return

        incoming = TgIncoming(
            text=text,
            photo_bytes=photo[0] if photo else None,
            photo_name=photo[1] if photo else f"tg_{message.message_id}.jpg",
            reply_to_max_preview=reply_preview,
            author=message.from_user.full_name,
            tg_message_id=message.message_id,
        )
        try:
            await self.on_incoming(incoming)
        except Exception:  # noqa: BLE001
            log.exception("Обработка входящего из Telegram упала")
            await _throttled_ack(self, message.message_id, "внутренняя ошибка, смотри логи")

    def _reply_from_max_preview(self, message: Message) -> str | None:
        """Ответ на сообщение, которое отправил ЭТОТ бот (значит из MAX)?
        Вернуть текст оригинала для цитирования, иначе None."""
        r = message.reply_to_message
        if r is None:
            return None
        if r.from_user is None or r.from_user.id != self.bot.id:
            return None
        body = (r.text or r.caption or "").strip()
        # Снять свою шапку вида «👤 Иван · 12:34», цитируем только текст.
        lines = body.splitlines()
        if lines and lines[0].lstrip().startswith("👤"):
            body = "\n".join(lines[1:]).lstrip("— \n").strip()
        if body:
            return body
        return "[Фото из MAX]" if r.photo else None

    async def _extract_photo(self, message: Message) -> tuple[bytes, str] | None:
        if not message.photo:
            return None
        biggest = message.photo[-1]
        try:
            tg_file = await self.bot.get_file(biggest.file_id)
            data = await self.download_bytes(tg_file.file_path)
            if not data:
                return None
            return data, f"tg_{message.message_id}.jpg"
        except Exception:  # noqa: BLE001
            log.exception("Не удалось скачать фото из Telegram")
            await _throttled_ack(self, message.message_id, "фото скачать не вышло")
            return None

    async def download_bytes(self, file_path: str) -> bytes:
        """Скачивание по file_path через НАШ API-бейс (то есть через прокси)."""
        stream = await self.bot.download_file(file_path)
        buf = io.BytesIO()
        # bot.download_file возвращает aiohttp StreamReader (или file-like).
        if hasattr(stream, "read"):
            while True:
                piece = await stream.read(256 * 1024) if asyncio.iscoroutinefunction(stream.read) \
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

    # ----------------------------------------------------------- outgoing
    async def send_text(self, text: str) -> Optional[int]:
        last_id: Optional[int] = None
        for chunk in split_text(text):
            sent = await self._send_with_retry(
                self.bot.send_message, chat_id=self.cfg.tg_chat_id, text=chunk
            )
            last_id = sent.message_id
        return last_id

    async def send_photo(self, data: bytes, caption: str,
                         filename: str = "max_photo.png") -> Optional[int]:
        caption = truncate(caption or "", MAX_CAPTION)
        try:
            sent = await self._send_with_retry(
                self.bot.send_photo,
                chat_id=self.cfg.tg_chat_id,
                photo=BufferedInputFile(data, filename=filename),
                caption=caption or None,
            )
            return sent.message_id
        except TelegramBadRequest as exc:
            log.error("send_photo отклонён (%s) — пробую send_document", exc)
            try:
                sent = await self.bot.send_document(
                    self.cfg.tg_chat_id,
                    BufferedInputFile(data, filename=filename),
                    caption=caption or None,
                )
                return sent.message_id
            except Exception:  # noqa: BLE001
                log.exception("Фото в Telegram доставить не удалось")
                return None

    async def _send_with_retry(self, fn, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return await fn(**kwargs)
            except TelegramRetryAfter as exc:
                wait_s = int(exc.retry_after) + 1
                log.warning("Telegram flood-limit: спим %d c", wait_s)
                await asyncio.sleep(wait_s)
            except TelegramBadRequest:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                wait_s = min(2 ** attempt * 2, 30)
                log.warning("Telegram send упал (%s), попытка %d/4, спим %d c",
                            exc, attempt + 1, wait_s)
                await asyncio.sleep(wait_s)
        raise RuntimeError(f"Telegram: 4 неудачные попытки: {last_exc}")

    async def notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.cfg.tg_chat_id, f"{self.cfg.tg_ack_prefix} {text}")
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отправить уведомление в TG: %s", text)

    async def show_typing(self, seconds: float = 6.0) -> None:
        try:
            for _ in range(int(seconds / 5) + 1):
                await self.bot.send_chat_action(self.cfg.tg_chat_id, "typing")
                await asyncio.sleep(min(5.0, seconds))
                seconds -= 5.0
                if seconds <= 0:
                    break
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ running
    async def prepare(self) -> None:
        me = await self.bot.get_me()
        log.info("Telegram: авторизовался @%s через %s", me.username, self.cfg.tg_api_base)

    async def run_polling(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        try:
            await self.bot.close()
        except Exception:  # noqa: BLE001
            pass


async def _throttled_ack(tg: TelegramBridge, tg_message_id: int, note: str) -> None:
    now = time.monotonic()
    if now - tg._last_ack.get(0, 0.0) < ACK_SPAM_WINDOW:
        return
    tg._last_ack[0] = now
    try:
        await tg.bot.send_message(
            tg.cfg.tg_chat_id,
            f"{tg.cfg.tg_ack_prefix} {note}",
            reply_to_message_id=tg_message_id,
        )
    except Exception:  # noqa: BLE001
        log.debug("ack не ушёл")
