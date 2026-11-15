"""
app/main.py
===========
Основной режим моста MAX ⇄ Telegram.

Запуск:  python -m app.main   (в контейнере — CMD по умолчанию)

Порядок старта:
    1. загрузка конфигурации из ENV;
    2. Telegram-бот (через ваш Nginx-реверс api.telegram.org);
    3. Chromium с persistent-профилем -> web.max.ru -> целевой чат;
    4. в ленту чата внедряется JS-мост, история помечается «переданной»;
    5. запускаются задачи: MAX-поллер, воркер, TG-polling, watchdog,
       ночная перезагрузка;
    6. SIGTERM/SIGINT — корректное завершение (сохранение профиля, стоп бота).

Любая фатальная ошибка (протухшая сессия MAX, умерший Chromium) приводит к
выходу с кодом 1 — docker `restart: unless-stopped` всё поднимет заново.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from .bridge import Bridge
from .browser import BrowserManager
from .config import load_config
from .max_dom import MaxAuthError, MaxWeb
from .store import Store
from .telegram_bot import TelegramBridge
from .utils import get_logger, setup_logging

log = get_logger("main")


class BridgeRuntime:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.store = Store(self.cfg.db_path)
        self.tg = TelegramBridge(self.cfg)
        self.browser = BrowserManager(self.cfg)
        self.max: MaxWeb | None = None
        self.bridge: Bridge | None = None
        self._tasks: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.tg.prepare()

        await self.browser.start()
        self.max = MaxWeb(self.browser.page, self.cfg)

        try:
            await self.max.open_max()
            await self.max.open_target_chat()
        except MaxAuthError as exc:
            log.error("MAX не авторизован: %s", exc)
            await self.tg.notify(
                f"⛔ MAX требует авторизации: {exc}\n"
                "Выполните: docker compose run --rm bridge python -m app.auth"
            )
            raise SystemExit(1)

        self.bridge = Bridge(self.cfg, self.browser, self.max, self.tg, self.store)
        self.tg.on_incoming = self.bridge.on_telegram_incoming

        async def _on_command(cmd: str) -> str:
            assert self.bridge is not None
            return self.bridge.status_report() if cmd == "status" else "?"

        self.tg.on_command = _on_command

        self._spawn(self.bridge.max_poller(), "max_poller")
        self._spawn(self.bridge.max_worker(), "max_worker")
        self._spawn(self.bridge.watchdog(), "watchdog")
        self._spawn(self.bridge.nightly_reload(), "nightly_reload")
        self._spawn(self.tg.run_polling(), "tg_polling")

        await self.tg.notify(
            f"🟢 Мост поднят. {self.bridge.status_report()}\n"
            f"Чат MAX: {self.cfg.max_chat_title or self.cfg.max_chat_url}"
        )
        log.info("Мост работает. /status — сводка, SIGTERM — graceful shutdown")
        await self._shutdown.wait()

    def _spawn(self, coro, name: str) -> None:
        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                log.info("Задача %s отменена", name)
                raise
            except MaxAuthError as exc:
                log.error("Фатальная ошибка авторизации MAX в %s: %s", name, exc)
                with contextlib.suppress(Exception):
                    await self.tg.notify(f"⛔ {name}: сессия MAX истекла — нужен app.auth")
                self._request_shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Задача %s упала — останавливаюсь, docker рестартует контейнер", name)
                self._request_shutdown()

        self._tasks.append(asyncio.create_task(runner(), name=name))

    def _request_shutdown(self) -> None:
        self._shutdown.set()

    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        log.info("Останавливаюсь…")
        if self.bridge:
            self.bridge.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            await self.tg.notify("🛑 Мост остановлен (см. логи контейнера)")
        await self.browser.stop()
        await self.tg.close()
        self.store.close()
        log.info("Все ресурсы освобождены, пока! 🦪")


def _install_signal_handlers(runtime: BridgeRuntime) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime._request_shutdown)


async def amain() -> int:
    setup_logging()
    try:
        runtime = BridgeRuntime()
    except SystemExit as exc:  # ошибки конфигурации из load_config
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
