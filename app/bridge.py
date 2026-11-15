"""
app/bridge.py
=============
Сердце моста: маршрутизация сообщений MAX <-> Telegram.

MAX -> Telegram:
    фоновый цикл читает JS-мост (max_dom), отбирает новые НЕсвои сообщения,
    прогоняет через дедуп (store), скачивает картинки внутри вкладки и шлёт
    в Telegram (подпись «👤 отправитель · время» + текст + фотоальбомом).

Telegram -> MAX:
    принимает TgIncoming от telegram_bot, защищает от эхо-петли, печатает
    «человеческими» задержками и отправляет в открытый чат MAX (reply или
    обычным сообщением, фото — через input[type=file]).

Отдельные задачи (asyncio.Task):
    * max_poller   — чтение ленты;
    * watchdog     — здоровье вкладки + переподключение после падений;
    * nightly      — суточная мягкая перезагрузка страницы в 03:00 (антиутечка
      памяти Chromium).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from .browser import BrowserManager
from .config import Config
from .max_dom import MaxAuthError, MaxMessage, MaxWeb
from .store import Store
from .telegram_bot import TelegramBridge, TgIncoming
from .utils import get_logger, human_pause, retry, truncate

log = get_logger("bridge")

MAX_CAPTION = 1000


@dataclass(slots=True)
class BridgeStats:
    max_to_tg: int = 0
    tg_to_max: int = 0
    images: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_max_msg_at: float = 0.0
    last_tg_msg_at: float = 0.0


class Bridge:
    def __init__(self, cfg: Config, browser: BrowserManager,
                 max_web: MaxWeb, tg: TelegramBridge, store: Store) -> None:
        self.cfg = cfg
        self.browser = browser
        self.max = max_web
        self.tg = tg
        self.store = store

        self.stats = BridgeStats(started_at=time.time())
        self._queue: asyncio.Queue[MaxMessage] = asyncio.Queue(maxsize=200)
        self._send_lock = asyncio.Lock()   # одна операция в браузере за раз
        self._recent_outbound: dict[str, float] = {}  # хэш текста -> время
        self._reloading = False
        self._stop = asyncio.Event()

    # =====================================================================
    #  MAX -> Telegram
    # =====================================================================
    async def max_poller(self) -> None:
        """Чтение дрена JS-моста; тяжёлую обработку кладём в очередь."""
        log.info("MAX-поллер запущен (interval=%.1fs)", self.cfg.poll_interval)
        while not self._stop.is_set():
            try:
                if self._reloading:
                    await asyncio.sleep(1.0)
                    continue
                if not await self.max.ensure_listening():
                    await asyncio.sleep(5.0)
                    continue
                messages = await self.max.poll()
                for msg in messages:
                    try:
                        self._queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        log.warning("Очередь переполнена — сообщение %s пропущено", msg.key)
            except MaxAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.debug("poll error: %s", exc)
                await asyncio.sleep(2.0)
            await asyncio.sleep(self.cfg.poll_interval)

    async def max_worker(self) -> None:
        while not self._stop.is_set():
            msg = await self._queue.get()
            try:
                await self._forward_max_to_tg(msg)
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("Не удалось обработать сообщение MAX %s", msg.key)
            finally:
                self._queue.task_done()

    def _should_skip(self, msg: MaxMessage) -> str | None:
        if not msg.text and not msg.images:
            return "пустое"
        if msg.mine:
            return "моё (mine-маркер)"
        if (self.cfg.max_self_name and msg.sender
                and self.cfg.max_self_name.lower() in msg.sender.lower()):
            return "моё (MAX_SELF_NAME)"
        if self.cfg.max_sender_filter and msg.sender:
            if not any(f in msg.sender.lower() for f in self.cfg.max_sender_filter):
                return f"отправитель {msg.sender!r} вне фильтра"
        # Защита от эхо-петли: текст, который мы сами недавно отправили в MAX.
        h = hashlib.sha256(msg.text.encode("utf-8")).hexdigest()[:16]
        sent_at = self._recent_outbound.get(h)
        if sent_at and time.time() - sent_at < 90:
            return "наш собственный ответ (эхо)"
        return None

    async def _forward_max_to_tg(self, msg: MaxMessage) -> None:
        reason = self._should_skip(msg)
        if reason:
            log.debug("MAX -> TG: пропуск (%s): %.60s", reason, msg.text)
            return

        if not self.store.mark_seen(msg.key):
            log.debug("MAX -> TG: дубль ключа %s", msg.key)
            return

        when = time.strftime("%d.%m %H:%M", time.localtime(msg.ts / 1000 if msg.ts else time.time()))
        header = f"👤 {truncate(msg.sender or 'Без имени', 60)} · {when}"

        # ---------- картинки ----------
        photos: list[tuple[bytes, str]] = []
        if self.cfg.max_download_images and msg.images:
            for img in msg.images[:4]:  # не более 4 картинок на сообщение
                blob = await self.max.download_image(img.src)
                if blob:
                    photos.append((blob, f"max_{msg.key[:12]}_{len(photos)}.png".replace("=", "_")))
                    self.stats.images += 1
                await asyncio.sleep(0.6)

        text_tail = truncate(msg.text, MAX_CAPTION) if msg.text else ""

        if photos:
            caption = header + (f"\n{text_tail}" if text_tail else "")
            for i, (blob, name) in enumerate(photos):
                # подпись только на первом фото альбома
                await self.tg.send_photo(blob, caption if i == 0 else "", filename=name)
                self.stats.max_to_tg += 1
                self.stats.last_max_msg_at = time.time()
                await asyncio.sleep(0.6)
            return

        if msg.text:
            footnote = ""
            if msg.images and not photos:
                footnote = "\n🖼 (изображения скачать не удалось)"
            tg_id = await self.tg.send_text(f"{header}\n{msg.text}{footnote}")
            if tg_id:
                self.store.remember_tg_to_max(tg_id, msg.key, msg.sender, msg.text)
            self.stats.max_to_tg += 1
            self.stats.last_max_msg_at = time.time()
            log.info("MAX -> TG: [%s] %.80s", msg.sender or "?", msg.text.replace("\n", " "))

    # =====================================================================
    #  Telegram -> MAX
    # =====================================================================
    async def on_telegram_incoming(self, incoming: TgIncoming) -> None:
        self.stats.last_tg_msg_at = time.time()
        log.info("TG -> MAX: от %r, %d байт фото=%s, reply=%s",
                 incoming.author, len(incoming.text), bool(incoming.photo_bytes),
                 bool(incoming.reply_to_max_preview))

        body = incoming.text
        sender_tag = f"— {incoming.author} (TG)"
        if not body and incoming.photo_bytes is None:
            return

        async with self._send_lock:
            try:
                # «бот печатает…» в TG, пока возимся с браузером
                typing = asyncio.create_task(self.tg.show_typing(10))
                try:
                    if incoming.photo_bytes:
                        caption = body or sender_tag
                        await self.max.send_photo(incoming.photo_bytes, caption)
                    elif incoming.reply_to_max_preview:
                        ok = await self.max.reply_text(incoming.reply_to_max_preview, body)
                        if not ok:
                            quote = truncate(incoming.reply_to_max_preview, 120)
                            await self.max.send_text(f"«{quote}»\n\n{body}\n{sender_tag}")
                    else:
                        await self.max.send_text(body)
                finally:
                    typing.cancel()

                self.stats.tg_to_max += 1
                # запоминаем, чтобы собственные сообщения не вернулись в TG
                if body:
                    self._remember_outbound(body)
                await self.tg.notify(f"отправлено в MAX ✅ ({incoming.author})")
            except MaxAuthError:
                await self.tg.notify("⛔ Сессия MAX истекла — нужна повторная авторизация "
                                     "`python -m app.auth`")
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("Отправка в MAX не удалась")
                await self.tg.notify(f"⛔ в MAX не ушло: {truncate(str(exc), 200)}")

    def _remember_outbound(self, text: str) -> None:
        now = time.time()
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self._recent_outbound[h] = now
        # чистим старые записи
        for k in [k for k, t in self._recent_outbound.items() if now - t > 300]:
            del self._recent_outbound[k]

    # =====================================================================
    #  Watchdog / ночная перезагрузка / статус
    # =====================================================================
    async def watchdog(self) -> None:
        """Раз в keepalive_interval проверяем, что вкладка жива; при падении —
        мягкий рестарт контекста (профиль на диске сохраняется)."""
        log.info("Watchdog запущен")
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.keepalive_interval)
            if self._reloading:
                continue
            try:
                alive = await self.max.health()
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                await self.max.keepalive()
                continue

            self.stats.errors += 1
            log.warning("Вкладка MAX не здорова — восстанавливаю…")
            try:
                await self.tg.notify("Вкладка MAX отвалилась, переподключаюсь…")
                await retry(
                    self.max.open_target_chat,
                    attempts=3,
                    base_delay=5.0,
                    retry_on=(Exception,),
                    what="переоткрытие чата MAX",
                    logger=log,
                )
                await self.tg.notify("Соединение с MAX восстановлено ✅")
            except MaxAuthError:
                await self.tg.notify("⛔ Сессия MAX истекла — выполните авторизацию заново")
                raise
            except Exception:  # noqa: BLE001
                log.exception("Восстановление не удалось — делаю мягкий рестарт браузера")
                try:
                    await self.browser.soft_restart()
                    self.max = MaxWeb(self.browser.page, self.cfg)
                    await self.max.open_target_chat()
                except Exception:  # noqa: BLE001
                    log.exception("И рестарт браузера не помог — watchdog продолжит пробовать")

    async def nightly_reload(self) -> None:
        """Суточная мягкая перезагрузка страницы в cfg.daily_reload_hour:00 —
        профилактика утечек памяти SPA Chromium при работе 24/7."""
        log.info("Ночной перезагрузчик: срабатывает в %02d:00", self.cfg.daily_reload_hour)
        while not self._stop.is_set():
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  self.cfg.daily_reload_hour, 0, 0, 0, 0, -1))
            if target <= time.time():
                target += 86400
            sleep_for = target - time.time()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                pass

            self._reloading = True
            try:
                await human_pause(0, 120)  # не в секунду в секунду
                await self.max.reload_page()
                log.info("Ночная перезагрузка страницы выполнена")
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("Ночная перезагрузка упала — watchdog подстрахует")
            finally:
                self._reloading = False

    def status_report(self) -> str:
        up = int(time.time() - self.stats.started_at)
        parts = [
            f"аптайм {up // 3600}ч{up % 3600 // 60}м",
            f"MAX→TG: {self.stats.max_to_tg} (фото {self.stats.images})",
            f"TG→MAX: {self.stats.tg_to_max}",
            f"ошибок: {self.stats.errors}",
            f"буфер: {self._queue.qsize()}",
        ]
        return ", ".join(parts)

    # =====================================================================
    def stop(self) -> None:
        self._stop.set()
