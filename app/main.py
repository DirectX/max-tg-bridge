"""
app/main.py  (v2)
=================
Точка сборки Multi-Account & Role-Based Bridge.

     ┌─────────────┐   fanout    ┌──────────┐   ┌──────────────────┐
     │ MAX worker 1│────────────►│          │   │ Владелец 1 ( reply→MAX )
     │ MAX worker 2│────────────►│ Telegram │──►│ Слушатели …      │
     │ MAX worker N│───┐         │ Core     │   └──────────────────┘
     └─────────────┘   │         │ (proxy)  │──► Админ: /admin-панель
              ▲        │         └──────────┘        ▲   ▲
              │        ▼                             │   │
        AccountManager ◄──────── FSM-авторизация ────┘───┘
        (супервизоры, изоляция падений)

Порядок включения роутеров ВАЖЕН:
    1) auth_router  — состояния AuthFlow (перехватывают ввод телефона/кода);
    2) admin_router — /admin, колбэки, AdminInput;
    3) user_router  — catch-all личных сообщений (последним!).

SIGTERM/SIGINT: останавливаем супервизоров (браузеры корректно закрывают
профили), затем Telegram, затем SQLite. docker restart поднимет всё сам.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from aiogram import Router

from .account_manager import AccountManager
from .backup import backup_scheduler
from .config import load_config
from .store import Store
from .tg import admin_handlers, auth_fsm, user_handlers
from .tg.bot import TelegramCore
from .utils import get_logger, setup_logging

log = get_logger("main")


class Runtime:
    def __init__(self) -> None:
        self.cfg = load_config(require_admin=True)
        self.store = Store(self.cfg.db_path)
        self.tg = TelegramCore(self.cfg)
        self.mgr = AccountManager(self.cfg, self.store, self.tg)
        self._shutdown = asyncio.Event()
        self._polling: asyncio.Task | None = None
        self._backup_task: asyncio.Task | None = None

    async def run(self) -> None:
        # ---- роутеры (порядок обязателен) --------------------------------
        auth_router = Router()
        auth = auth_fsm.register(auth_router, self.cfg, self.store, self.tg, self.mgr)

        admin_router = Router()
        admin_handlers.register(admin_router, self.cfg, self.store, self.tg,
                                self.mgr, auth)

        user_router = Router()
        user_handlers.register(user_router, self.cfg, self.store, self.tg, self.mgr)

        self.tg.dp.include_router(auth_router)
        self.tg.dp.include_router(admin_router)
        self.tg.dp.include_router(user_router)

        # ---- старт --------------------------------------------------------
        await self.tg.prepare()
        await self.mgr.start_all()

        self._polling = asyncio.create_task(self.tg.run_polling(), name="tg_polling")
        # v2.2: суточный бэкап (04:00 — после ночных релоадов страниц).
        # Падение планировщика не роняет мост: только лог + уведомление админу.
        self._backup_task = asyncio.create_task(
            self._guarded_backup(), name="backup_scheduler")
        log.info("Мост v2 работает. Аккаунтов в работе: %s",
                 self.mgr.running_ids())
        await self._shutdown.wait()

    async def _guarded_backup(self) -> None:
        try:
            await backup_scheduler(self.cfg, self.store, self.tg, self._shutdown)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Бэкап-планировщик упал — мост продолжает работать")
            with contextlib.suppress(Exception):
                await self.tg.notify_admin(
                    "⚠️ Планировщик бэкапов упал (см. логи). Ручной бэкап: /backup")

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def shutdown(self) -> None:
        log.info("Останавливаюсь…")
        if self._polling:
            self._polling.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._polling
        # планировщик бэкапов сам выходит по _shutdown, но если он сейчас
        # посреди сборки архива — гасим и ждём: поток читает store, а store
        # закрывается строкой ниже
        if self._backup_task:
            self._backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._backup_task
        await self.mgr.shutdown()
        await self.tg.close()
        self.store.close()
        log.info("Все ресурсы освобождены")


def _install_signal_handlers(runtime: Runtime) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.request_shutdown)


async def amain() -> int:
    setup_logging()
    try:
        runtime = Runtime()
    except SystemExit as exc:      # ошибки конфигурации из load_config
        print(exc)
        return int(exc.code or 1)
    _install_signal_handlers(runtime)
    try:
        await runtime.run()
        return 0
    except SystemExit as exc:
        return int(exc.code or 1)
    except Exception:  # noqa: BLE001
        log.exception("Фатальная ошибка")
        return 1
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
