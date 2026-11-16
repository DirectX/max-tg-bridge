"""
app/account_manager.py
======================
Супервизор аккаунтов MAX (требование: независимые asyncio.Task, падение
одного аккаунта не роняет остальные).

Ответственности:
  * жизненный цикл воркеров (bridge.AccountWorker): запуск/остановка/удаление,
    авторестарт с экспоненциальным backoff при НЕавторизационных падениях;
  * MaxAuthError => статус auth_required + уведомление админу кнопкой
    «Переавторизовать» (дальше эстафету принимает auth_fsm);
  * интерактивные сессии авторизации (их ведёт app/tg/auth_fsm.py через
    методы begin_auth/…/auth_finalize) — браузер авторизации и браузер
    воркера ОДНОГО аккаунта никогда не работают одновременно (общий профиль!);
  * агрегированный статус для админ-панели.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import AccountWorker, Outgoing
from .browser import AccountBrowser
from .config import Config
from .max_dom import AccountTarget, MaxAuthError, MaxWeb
from .store import Account, Store
from .tg.bot import TelegramCore
from .utils import get_logger

log = get_logger("manager")

MAX_BACKOFF_S = 60.0


@dataclass
class AuthSession:
    """Идёт первичный вход / переавторизация: браузер-«невидимка» открыт,
    FSM (или CLI) ведёт диалог."""
    account: Account
    is_new: bool
    browser: AccountBrowser
    maxweb: MaxWeb
    idle_task: asyncio.Task | None = None
    meta: dict = field(default_factory=dict)


class AccountManager:
    def __init__(self, cfg: Config, store: Store, tg: TelegramCore) -> None:
        self.cfg = cfg
        self.store = store
        self.tg = tg
        self._workers: dict[int, AccountWorker] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._auth: dict[int, AuthSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._restart_counts: dict[int, int] = {}
        self._stopping = False
        self.started_at = time.time()

    # ================================================================ helpers
    def _lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def get_worker(self, account_id: int) -> AccountWorker | None:
        return self._workers.get(account_id)

    def running_ids(self) -> list[int]:
        return sorted(self._workers)

    # ================================================================ start
    async def start_all(self) -> None:
        """Вызывается один раз из main: поднимаем всё, что помечено active."""
        accounts = self.store.list_accounts()
        for acc in accounts:
            if acc.status == "authenticating":
                # процесс умер на середине входа — честнее попросить логин заново
                self.store.update_account(acc.id, status="auth_required")
                await self.tg.notify_admin(
                    f"🔑 Аккаунт «{acc.name}» был в середине авторизации в момент "
                    "рестарта — выполните /admin → Переавторизовать."
                )
                continue
            if acc.status == "active":
                self._spawn_supervisor(acc)
        if accounts:
            log.info("Супервизор: %d аккаунт(ов), %d в работе",
                     len(accounts), len(self._tasks))
            await self.tg.notify_admin(
                "🟢 Бридж v2 поднят: " +
                ", ".join(f"«{a.name}» {a.status}" for a in accounts)
            )

    async def shutdown(self) -> None:
        self._stopping = True
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._workers.clear()
        for account_id in list(self._auth):
            await self._close_auth_browser(account_id)

    # ============================================================ supervisor
    def _spawn_supervisor(self, acc: Account) -> None:
        if acc.id in self._tasks:
            return
        self._tasks[acc.id] = asyncio.create_task(
            self._supervise(acc.id), name=f"supervisor:{acc.id}")

    async def _supervise(self, account_id: int) -> None:
        """Вечный присмотр за одним аккаунтом. Ничего общего с остальными."""
        backoff = 2.0
        try:
            while not self._stopping:
                acc = self.store.get_account(account_id)
                if acc is None or acc.status != "active":
                    return
                worker = AccountWorker(self.cfg, self.store, self.tg, acc)
                self._workers[account_id] = worker
                fatal: Exception | None = None
                try:
                    await worker.start()
                    await worker.wait()
                except asyncio.CancelledError:
                    await worker.stop()
                    raise
                except MaxAuthError as exc:
                    await worker.stop()
                    self._workers.pop(account_id, None)
                    self.store.update_account(account_id, status="auth_required")
                    log.warning("Аккаунт #%s требует авторизации: %s", account_id, exc)
                    await self._notify_auth_required(self.store.get_account(account_id))
                    return  # без рестарта: ждём человека
                except Exception as exc:  # noqa: BLE001
                    fatal = exc
                finally:
                    if account_id in self._workers:
                        try:
                            await self._workers.pop(account_id).stop()
                        except Exception:  # noqa: BLE001
                            pass

                self._restart_counts[account_id] = self._restart_counts.get(account_id, 0) + 1
                n = self._restart_counts[account_id]
                log.error("Аккаунт #%d упал (%s), рестарт через %.0f c (попытка %d)",
                          account_id, fatal, backoff, n)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        except asyncio.CancelledError:
            log.info("Супервизор аккаунта #%d остановлен", account_id)
        finally:
            self._tasks.pop(account_id, None)
            self._workers.pop(account_id, None)

    async def _notify_auth_required(self, acc: Account | None) -> None:
        if acc is None:
            return
        from aiogram.types import InlineKeyboardMarkup
        from .tg.keyboards import btn
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            btn(f"🔑 Переавторизовать «{acc.name}»", callback_data=f"relogin:{acc.id}")]])
        await self.tg.notify_admin(
            f"🔴 Сессия MAX аккаунта «{acc.name}» истекла "
            f"(чат: {acc.max_chat_title or '—'}).\n"
            "Нажмите кнопку и следуйте инструкциям бота — профиль сохранён, "
            "понадобится только новый код из СМС.", reply_markup=kb)

    # ============================================================ lifecycle
    async def create_account(self, name: str, chat_title: str, *,
                             chat_url: str = "", owner_tg_id: int | None = None,
                             phone: str = "", self_name: str = "") -> Account:
        name = name.strip()
        if not name:
            raise ValueError("Пустое название")
        if self.store.get_account_by_name(name):
            raise ValueError(f"Название «{name}» уже занято")
        acc = self.store.create_account(
            name, phone=phone.strip(), chat_title=chat_title.strip(),
            chat_url=chat_url.strip(), self_name=self_name.strip(),
            owner_tg_id=owner_tg_id, status="stopped", profile_path="")
        self.store.update_account(acc.id, profile_path=str(self.cfg.account_profile_dir(acc.id)))
        acc = self.store.get_account(acc.id)
        assert acc is not None
        return acc

    async def start_account(self, account_id: int) -> str:
        async with self._lock(account_id):
            acc = self.store.get_account(account_id)
            if acc is None:
                return "Аккаунт не найден"
            if account_id in self._auth:
                return "Идёт авторизация — дождитесь её завершения"
            if acc.id in self._tasks:
                return "Уже запущен"
            if not acc.max_chat_title and not acc.max_chat_url:
                return "Не задан чат MAX (название или URL) — сначала в /admin"
            self.store.update_account(account_id, status="active")
            self._spawn_supervisor(acc)
            return f"Аккаунт «{acc.name}» запускается"

    async def _stop_locked(self, account_id: int) -> None:
        """Остановить супервизор аккаунта. Вызывать ТОЛЬКО под self._lock()."""
        task = self._tasks.pop(account_id, None)
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.pop(account_id, None)

    async def stop_account(self, account_id: int) -> str:
        async with self._lock(account_id):
            acc = self.store.get_account(account_id)
            if acc is None:
                return "Аккаунт не найден"
            await self._stop_locked(account_id)
            self.store.update_account(account_id, status="stopped")
            return f"Аккаунт «{acc.name}» остановлен"

    async def delete_account(self, account_id: int) -> str:
        async with self._lock(account_id):
            acc = self.store.get_account(account_id)
            if acc is None:
                return "Аккаунт не найден"
            await self._stop_locked(account_id)
            await self._close_auth_browser(account_id)

            # Профиль не удаляем сразу — переезжает в .trash (можно откатить).
            profile = Path(acc.profile_path) if acc.profile_path \
                else self.cfg.account_profile_dir(account_id)
            if profile.exists():
                self.cfg.trash_root.mkdir(parents=True, exist_ok=True)
                dest = self.cfg.trash_root / f"account_{account_id}_{int(time.time())}"
                try:
                    shutil.move(str(profile), str(dest))
                except OSError as exc:
                    log.warning("Профиль %s в trash не переехал: %s", profile, exc)
            self.store.delete_account(account_id)
            self._locks.pop(account_id, None)
            self._restart_counts.pop(account_id, None)
            return (f"Аккаунт «{acc.name}» удалён (профиль — в "
                    f"{self.cfg.trash_root}/, слушатели и маппинги стёрты)")

    # ========================================================= исходящие TG->MAX
    def submit_outgoing(self, account_id: int, out: Outgoing) -> tuple[bool, str]:
        worker = self._workers.get(account_id)
        if worker is None:
            acc = self.store.get_account(account_id)
            state = acc.status if acc else "неизвестен"
            return False, f"Аккаунт не запущен (статус: {state})"
        if worker.submit_outgoing(out):
            return True, "Отправляю в MAX…"
        return False, "Очередь переполнена — попробуйте позже"

    # ============================================================ авторизация
    def get_auth(self, account_id: int) -> AuthSession | None:
        return self._auth.get(account_id)

    async def begin_auth(self, account_id: int, *, is_new: bool) -> str:
        """Открывает браузер авторизации поверх профиля аккаунта.
        Возвращает 'login_form' | 'logged_in' | 'unknown'."""
        async with self._lock(account_id):
            acc = self.store.get_account(account_id)
            if acc is None:
                raise ValueError("Аккаунт не найден")
            if account_id in self._auth:
                raise RuntimeError("Для аккаунта уже идёт авторизация")
            if account_id in self._tasks:
                # общий профиль: воркер должен быть остановлен ДО входа
                await self._stop_locked(account_id)

            if acc.status != "authenticating":
                self.store.update_account(account_id, status="authenticating")

            target = AccountTarget(
                account_id=acc.id, chat_title=acc.max_chat_title,
                chat_url=acc.max_chat_url, self_name=acc.max_self_name,
                tmp_dir=self.cfg.tmp_dir, max_image_bytes=self.cfg.max_image_bytes,
            )
            browser = AccountBrowser(self.cfg, account_id=acc.id, account_name=acc.name)
            try:
                await browser.start()
                maxweb = MaxWeb(browser.page, self.cfg, target)
                state = await maxweb.goto_home()
            except asyncio.CancelledError:
                # админ нажал «отмена», пока стартовал Chromium: без очистки
                # останется сиротой процесс + занятый профиль аккаунта
                self._log.warning("begin_auth #%d отменён — гашу браузер-сироту", account_id)
                with contextlib.suppress(Exception):
                    await browser.stop()
                self.store.update_account(
                    account_id, status="stopped" if is_new else acc.status)
                raise

            session = AuthSession(account=acc, is_new=is_new, browser=browser, maxweb=maxweb)
            session.idle_task = asyncio.create_task(self._auth_idle_guard(account_id))
            self._auth[account_id] = session
            return state

    async def auth_submit_phone(self, account_id: int, phone: str):
        sess = self._require_auth(account_id)
        self._touch_auth(sess)
        return await sess.maxweb.submit_phone(phone)

    async def auth_submit_code(self, account_id: int, code: str):
        sess = self._require_auth(account_id)
        self._touch_auth(sess)
        return await sess.maxweb.submit_code(code)

    async def auth_wait_logged_in(self, account_id: int, timeout_s: float = 90) -> bool:
        sess = self._require_auth(account_id)
        self._touch_auth(sess)
        return await sess.maxweb.wait_logged_in(timeout_s)

    async def auth_captcha(self, account_id: int) -> bool:
        sess = self._require_auth(account_id)
        return await sess.maxweb.detect_captcha()

    async def auth_finalize(self, account_id: int) -> Account:
        """Успешный вход: открываем целевой чат, закрываем браузер входа,
        запускаем воркер. Бросает MaxChatNotFound с готовым скриншотом-диагнозом."""
        async with self._lock(account_id):
            sess = self._require_auth(account_id)
            try:
                await sess.maxweb.open_target_chat()
                account = sess.account
                await self._close_auth_browser(account_id)
                self.store.update_account(account_id, status="active")
                acc = self.store.get_account(account_id)
                assert acc is not None
                self._spawn_supervisor(acc)
                await self.tg.notify_admin(f"🟢 Аккаунт «{acc.name}» авторизован и работает.")
                return acc
            except Exception:
                # оставляем сессию открытой — пусть повторят код/найдут селектор
                raise

    async def auth_abort(self, account_id: int, reason: str = "") -> str:
        sess = self._auth.get(account_id)
        is_new = bool(sess and sess.is_new)
        acc = self.store.get_account(account_id)
        await self._close_auth_browser(account_id)
        if acc is None:
            return "Аккаунт уже удалён"
        if is_new:
            # аккаунт создан этой сессией входа и никогда не работал — сносим следы
            profile = Path(acc.profile_path) if acc.profile_path \
                else self.cfg.account_profile_dir(account_id)
            self.store.delete_account(account_id)
            if profile.exists():
                shutil.rmtree(profile, ignore_errors=True)
            await self.tg.notify_admin(
                f"Новый аккаунт «{acc.name}» удалён (вход не завершён: "
                f"{reason or 'отмена'}).")
        else:
            self.store.update_account(account_id, status="auth_required")
        return f"Авторизация аккаунта «{acc.name}» отменена. {reason}".strip()

    async def _close_auth_browser(self, account_id: int) -> None:
        sess = self._auth.pop(account_id, None)
        if sess is None:
            return
        if sess.idle_task:
            sess.idle_task.cancel()
        try:
            await sess.browser.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("браузер авторизации не закрылся корректно: %s", exc)

    def _require_auth(self, account_id: int) -> AuthSession:
        sess = self._auth.get(account_id)
        if sess is None:
            raise RuntimeError(f"Нет активной сессии авторизации для #{account_id}")
        return sess

    def _touch_auth(self, sess: AuthSession) -> None:
        sess.meta["last_activity"] = time.time()

    async def _auth_idle_guard(self, account_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                sess = self._auth.get(account_id)
                if sess is None:
                    return
                idle = time.time() - sess.meta.get("last_activity", time.time())
                if idle > self.cfg.auth_idle_timeout:
                    log.info("Сессия авторизации #%d простаивает — закрываю", account_id)
                    await self.auth_abort(account_id, "таймаут ожидания ввода админа")
                    return
        except asyncio.CancelledError:
            pass

    # ================================================================ статус
    def snapshot(self) -> list[dict]:
        """Для админ-панели и /status."""
        out: list[dict] = []
        for acc in self.store.list_accounts():
            worker = self._workers.get(acc.id)
            stats = worker.stats.render() if worker else ""
            persisted = self.store.get_stats(acc.id)
            out.append({
                "id": acc.id,
                "name": acc.name,
                "status": acc.status,
                "auth": acc.id in self._auth,
                "chat": acc.max_chat_title or acc.max_chat_url or "—",
                "owner": acc.owner_tg_id,
                "listeners": len(self.store.list_listeners(acc.id)),
                "stats": stats,
                "persisted": persisted,
            })
        return out

    def uptime_str(self) -> str:
        up = int(time.time() - self.started_at)
        return f"{up // 3600}ч {up % 3600 // 60}м"
