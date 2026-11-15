"""
app/bridge.py  (v2)
===================
Мост ОДНОГО аккаунта MAX: MAX -> (Владелец + Слушатели) и обратно.

Отличия от v1:
  * инстанс на аккаунт: свои AccountBrowser (изолированный профиль),
    MaxWeb, задачи asyncio, статистика, эхо-детектор;
  * MAX -> Telegram — ФАН-АУТ: каждое сообщение уходит В ЛИЧКУ каждому
    адресату (Владелец + Список слушателей). Для каждого получателя
    фиксируется своя пара (tg_user_id, tg_message_id) -> max_message_key
    в store.message_map — именно по reply'ю на СВОЁ сообщение Владелец
    сможет ответить в MAX;
  * слушатели read-only: их ответы обрабатывает user_handlers (сюда не доходят);
  * ночная мягкая перезагрузка — со СДВИГОМ от базового часа
    (base + account_id * spread), чтобы N браузеров не перезагружать в такт;
  * падение этого объекта не касается остальных аккаунтов — решает
    account_manager (супервизор с backoff).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from .browser import AccountBrowser
from .config import Config
from .max_dom import AccountTarget, MaxAuthError, MaxMessage, MaxWeb
from .store import Account, Store
from .tg.bot import TelegramCore
from .utils import get_logger, human_pause, retry, truncate

log = get_logger("bridge")

MAX_CAPTION = 1000
ECHO_TTL_S = 90.0


@dataclass(slots=True)
class Outgoing:
    """Сообщение из Telegram, которое Владелец просит отправить в MAX."""
    text: str
    photo_bytes: bytes | None = None
    photo_name: str = "tg_photo.jpg"
    reply_preview: str | None = None    # текст исходного сообщения MAX (для реплея/цитаты)
    owner_chat_id: int = 0              # куда вернуть ack
    author_name: str = "?"


@dataclass(slots=True)
class WorkerStats:
    max_to_tg: int = 0
    images: int = 0
    tg_to_max: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_max_msg_at: float = 0.0

    def render(self) -> str:
        up = int(time.time() - self.started_at) if self.started_at else 0
        return (f"аптайм {up // 3600}ч{up % 3600 // 60}м · "
                f"MAX→TG {self.max_to_tg} (фото {self.images}) · "
                f"TG→MAX {self.tg_to_max} · ошибок {self.errors}")


class AccountWorker:
    def __init__(self, cfg: Config, store: Store, tg: TelegramCore,
                 account: Account) -> None:
        self.cfg = cfg
        self.store = store
        self.tg = tg
        self.account = account
        self.log = get_logger(f"bridge.a{account.id}")

        self.target = AccountTarget(
            account_id=account.id,
            chat_title=account.max_chat_title,
            chat_url=account.max_chat_url,
            self_name=account.max_self_name,
            tmp_dir=cfg.tmp_dir,
            max_image_bytes=cfg.max_image_bytes,
        )
        self.browser = AccountBrowser(cfg, account_id=account.id,
                                      account_name=account.name)
        self.max: MaxWeb | None = None
        self.stats = WorkerStats()

        self._queue: asyncio.Queue[Outgoing] = asyncio.Queue(maxsize=100)
        self._send_lock = asyncio.Lock()
        self._recent_outbound: dict[str, float] = {}
        self._reloading = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ============================================================ lifecycle
    @property
    def label(self) -> str:
        return self.account.name or f"#{self.account.id}"

    async def start(self) -> None:
        """Блокирующий запуск: браузер -> чат -> задачи. Падает MaxAuthError,
        если профиль неавторизован (менеджер переведёт аккаунт в auth_required)."""
        await self.browser.start()
        try:
            self.max = MaxWeb(self.browser.page, self.cfg, self.target)
            await self.max.open_max()
            await self.max.open_target_chat()
        except MaxAuthError:
            await self.browser.stop()
            raise

        self.stats.started_at = time.time()
        self._spawn(self._poller(), "poller")
        self._spawn(self._forwarder(), "forwarder")
        self._spawn(self._sender(), "sender")
        self._spawn(self._watchdog(), "watchdog")
        self._spawn(self._nightly(), "nightly")
        self.log.info("Воркер аккаунта «%s» запущен", self.label)

    def _spawn(self, coro, name: str) -> None:
        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except MaxAuthError as exc:
                self.log.error("Сессия MAX аккаунта «%s» истекла: %s", self.label, exc)
                self._stop.set()
                raise
            except Exception:
                self.log.exception("Задача %s аккаунта «%s» упала", name, self.label)
                self._stop.set()
                raise

        t = asyncio.create_task(runner(), name=f"{self.label}:{name}")
        self._tasks.append(t)

    async def wait(self) -> None:
        """До первой фатальной задачи/stop()."""
        stop_waiter = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            [*self._tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        stop_waiter.cancel()
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc:
                raise exc

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.browser.stop()
        self.log.info("Воркер аккаунта «%s» остановлен", self.label)

    # ================================================================= MAX->TG
    def submit_outgoing(self, out: Outgoing) -> bool:
        try:
            self._queue.put_nowait(out)
            return True
        except asyncio.QueueFull:
            return False

    async def _poller(self) -> None:
        self.log.info("Poller запущен (interval=%.1fs)", self.cfg.poll_interval)
        assert self.max is not None
        while not self._stop.is_set():
            try:
                if self._reloading:
                    await asyncio.sleep(1.0)
                    continue
                if not await self.max.ensure_listening():
                    await asyncio.sleep(5.0)
                    continue
                for msg in await self.max.poll():
                    try:
                        self._queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        self.log.warning("Очередь переполнена — сообщение пропущено")
            except MaxAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.log.debug("poll error: %s", exc)
                await asyncio.sleep(2.0)
            await asyncio.sleep(self.cfg.poll_interval)

    async def _forwarder(self) -> None:
        while not self._stop.is_set():
            msg = await self._queue.get()
            try:
                await self._forward_max_to_recipients(msg)
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                self.log.exception("Ошибка фан-аута сообщения %s", msg.key)
            finally:
                self._queue.task_done()

    def _should_skip(self, msg: MaxMessage) -> str | None:
        if not msg.text and not msg.images:
            return "пустое"
        if msg.mine:
            return "моё (mine)"
        if (self.account.max_self_name and msg.sender
                and self.account.max_self_name.lower() in msg.sender.lower()):
            return "моё (self name)"
        if self.cfg.max_sender_filter and msg.sender:
            if not any(f in msg.sender.lower() for f in self.cfg.max_sender_filter):
                return f"отправитель вне фильтра ({msg.sender!r})"
        h = hashlib.sha256(msg.text.encode()).hexdigest()[:16]
        sent_at = self._recent_outbound.get(h)
        if sent_at and time.time() - sent_at < ECHO_TTL_S:
            return "эхо нашего ответа"
        return None

    async def _forward_max_to_recipients(self, msg: MaxMessage) -> None:
        reason = self._should_skip(msg)
        if reason:
            self.log.debug("MAX->TG пропуск (%s): %.60s", reason, msg.text)
            return
        if not self.store.mark_seen(self.account.id, msg.key):
            self.log.debug("MAX->TG дубль ключа %s", msg.key)
            return

        owner, listeners = self.store.recipients(self.account.id)
        recipients = ([owner] if owner else []) + [u for u in listeners if u != owner]
        if not recipients:
            self.log.info("Аккаунту «%s» некому ничего слать (нет владельца/слушателей)",
                          self.label)
            return

        when = time.strftime("%d.%m %H:%M",
                             time.localtime(msg.ts / 1000 if msg.ts else time.time()))
        header = f"👤 {truncate(msg.sender or 'Без имени', 60)} · {when}"
        text_tail = truncate(msg.text, MAX_CAPTION) if msg.text else ""
        preview_src = msg.text or "[Фото из MAX]"

        # ---------- картинки (один раз, на аккаунт) ----------
        photos: list[tuple[bytes, str]] = []
        if self.cfg.max_download_images and msg.images and self.max:
            for img in msg.images[:4]:
                blob = await self.max.download_image(img.src)
                if blob:
                    safe_key = msg.key[:10].replace("=", "_").replace("/", "-")
                    photos.append((blob,
                                   f"max_a{self.account.id}_{safe_key}_{len(photos)}.png"))
                    self.stats.images += 1
                    self.store.bump_stat(self.account.id, "images")
                await asyncio.sleep(0.6)

        # ---------- фан-аут каждому получателю ----------
        for uid in recipients:
            try:
                if photos:
                    caption = header + (f"\n{text_tail}" if text_tail else "")
                    for i, (blob, name) in enumerate(photos):
                        sent_id = await self.tg.send_photo(
                            uid, blob, caption if i == 0 else "", filename=name)
                        if sent_id:
                            self._remember_map(uid, sent_id, msg, preview_src)
                        await asyncio.sleep(0.4)
                    self.stats.max_to_tg += 1
                    self.store.bump_stat(self.account.id, "max_to_tg")
                    continue

                if not msg.text:
                    # картинка есть, но скачать в headless-режиме не удалось:
                    # шлём текстовый вариант-заглушку, чтобы чат не «замолчал»
                    self.log.warning("[%s] фото %s скачать не удалось — отправляю заглушку",
                                     self.label, msg.key)
                    sent_id = await self.tg.send_text(
                        uid, f"{header}\n🖼 [изображение недоступно]")
                    if sent_id:
                        self._remember_map(uid, sent_id, msg, "[Фото из MAX]")
                    self.stats.max_to_tg += 1
                    self.store.bump_stat(self.account.id, "max_to_tg")
                    continue

                if msg.text:
                    footnote = ""
                    if msg.images and not photos:
                        footnote = "\n🖼 (изображения скачать не удалось)"
                    # send_text сам нарежет длинные сообщения по 4096;
                    # в map пишем id ПЕРВОго чанка — на него и отвечают.
                    sent_id = await self.tg.send_text(
                        uid, f"{header}\n{truncate(msg.text, 8000)}{footnote}")
                    if sent_id:
                        self._remember_map(uid, sent_id, msg, preview_src)
                    self.stats.max_to_tg += 1
                    self.store.bump_stat(self.account.id, "max_to_tg")
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                self.log.exception("Фан-аут пользователю %s не удался", uid)

        self.stats.last_max_msg_at = time.time()
        self.log.info("MAX->TG [%s] %.80s → %d получ.",
                      msg.sender or "?", msg.text.replace("\n", " "), len(recipients))

    def _remember_map(self, uid: int, tg_message_id: int, msg: MaxMessage,
                      preview_src: str) -> None:
        self.store.remember_message(
            self.account.id, uid, tg_message_id, msg.key, msg.sender,
            truncate(preview_src, 200))

    # ================================================================= TG->MAX
    async def _sender(self) -> None:
        while not self._stop.is_set():
            out = await self._queue.get()
            try:
                await self._send_to_max(out)
            except MaxAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.log.exception("Отправка в MAX не удалась")
                if out.owner_chat_id:
                    await self.tg.send_text(
                        out.owner_chat_id,
                        f"⛔ в MAX не ушло: {truncate(str(exc), 200)}")

    async def _send_to_max(self, out: Outgoing) -> None:
        assert self.max is not None
        sender_tag = f"— {out.author_name} (TG)"
        async with self._send_lock:
            typing = asyncio.create_task(self._typing_loop(out.owner_chat_id))
            try:
                if out.photo_bytes:
                    caption = out.text or sender_tag
                    await self.max.send_photo(out.photo_bytes, caption, out.photo_name)
                elif out.reply_preview:
                    ok = await self.max.reply_text(out.reply_preview, out.text)
                    if not ok:
                        quote = truncate(out.reply_preview, 120)
                        await self.max.send_text(
                            f"«{quote}»\n\n{out.text}\n{sender_tag}")
                else:
                    await self.max.send_text(out.text)
            finally:
                typing.cancel()

        self.stats.tg_to_max += 1
        self.store.bump_stat(self.account.id, "tg_to_max")
        if out.text:
            self._remember_outbound(out.text)
        if out.owner_chat_id:
            await self.tg.send_text(
                out.owner_chat_id, f"✅ отправлено в MAX ({out.author_name})")

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            for _ in range(3):
                if chat_id:
                    await self.tg.send_typing(chat_id)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    def _remember_outbound(self, text: str) -> None:
        now = time.time()
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        self._recent_outbound[h] = now
        for k in [k for k, t in self._recent_outbound.items() if now - t > 300]:
            del self._recent_outbound[k]

    # =============================================================== watchdog
    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.keepalive_interval)
            if self._reloading or self.max is None:
                continue
            try:
                alive = await self.max.health()
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                await self.max.keepalive()
                continue

            self.stats.errors += 1
            self.log.warning("Вкладка MAX «%s» не здорова — восстанавливаю…", self.label)
            try:
                await retry(self.max.open_target_chat, attempts=3, base_delay=5.0,
                            what=f"переоткрытие чата {self.label}", logger=self.log)
                continue
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.log.warning("Мягкий рестарт Chromium «%s»", self.label)
            try:
                await self.browser.soft_restart()
                self.max = MaxWeb(self.browser.page, self.cfg, self.target)
                await self.max.open_target_chat()
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.log.error("Восстановление «%s» не удалось — пробуем дальше", self.label)

    # ================================================================ nightly
    async def _nightly(self) -> None:
        """Суточная мягкая перезагрузка со сдвигом: базовый час + id*spread минут."""
        offset_min = self.account.id * self.cfg.daily_reload_spread_min
        self.log.info("Ночной перезагрузчик: %02d:00 + %d мин",
                      self.cfg.daily_reload_hour, offset_min % 60)
        while not self._stop.is_set():
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  self.cfg.daily_reload_hour, 0, 0, 0, 0, -1))
            target += offset_min * 60
            if target <= time.time():
                target += 86400
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=target - time.time())
                return
            except asyncio.TimeoutError:
                pass
            if self.max is None:
                continue
            self._reloading = True
            try:
                await human_pause(0, 120)
                await self.max.reload_page()
                self.log.info("Ночная перезагрузка выполнена")
            except MaxAuthError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                self.log.exception("Ночная перезагрузка упала — watchdog подстрахует")
            finally:
                self._reloading = False
