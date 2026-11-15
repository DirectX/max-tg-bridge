"""
app/auth.py
===========
ПЕРВИЧНАЯ АВТОРИЗАЦИЯ MAX БЕЗ GUI (headless-режим через терминал).

Запуск ( Telegram-переменные не нужны):

    docker compose run --rm bridge python -m app.auth            # шаг за шагом
    docker compose run --rm bridge python -m app.auth --manual   # «всё руками» через VNC/скриншоты

Алгоритм --auto (по умолчанию):
    1. Playwright (headless) открывает web.max.ru persistent-профилем;
    2. если мы уже авторизованы — просто находит чат и сохраняет профиль;
    3. иначе запрашивает телефон в терминале -> вводит на странице;
    4. ждёт SMS-код в терминале -> вводит посимвольно в активное поле;
    5. ждёт входа, открывает целевой чат (по URL или через поиск),
       делает скриншоты каждого шага в ./data/logs/ и сохраняет
       cookies/IndexedDB в ./data/chrome_profile.

Если MAX покажет капчу/QR — скрипт сохранит screenshot и подскажет
воспользоваться режимом --manual + временный VNC (см. README, раздел
«Авторизация с глазками»).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout

from .browser import BrowserManager
from .config import Config, load_config
from .max_dom import MaxWeb
from .selectors import (
    CAPTCHA_MARKERS,
    LOGGED_IN_MARKERS,
    PHONE_INPUT,
    PHONE_SUBMIT_BUTTON,
    SMS_INPUT,
    SMS_SUBMIT_BUTTON,
)
from .utils import get_logger, jitter, setup_logging

log = get_logger("auth")

SCREENSHOTS = Path("/data/logs")


def _ts_name(kind: str) -> str:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    return str(SCREENSHOTS / f"auth_{kind}_{datetime.now():%H%M%S}.png")


def normalize_phone(raw: str) -> str:
    """Приводит ввод пользователя к формату +7XXXXXXXXXX (формат РФ)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    if len(digits) == 11 and digits[0] != "7":  # казахстан и т.п.
        return "+" + digits
    raise ValueError(f"Не понимаю номер {raw!r} — введите +7 и 10 цифр")


async def shot(page, kind: str) -> str:
    path = _ts_name(kind)
    try:
        await page.screenshot(path=path, full_page=False)
        log.info("Скриншот сохранён: %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Скриншот не сделан: %s", exc)
    return path


async def find_first(page, selectors: list[str], timeout_ms: int = 8000):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:  # noqa: BLE001
            continue
    return None


async def detect_captcha(page) -> bool:
    for sel in CAPTCHA_MARKERS:
        try:
            if await page.locator(sel).first.count() > 0:
                return True
        except Exception:
            continue
    return False


async def already_logged_in(page) -> bool:
    marker = await find_first(page, LOGGED_IN_MARKERS, timeout_ms=12_000)
    return marker is not None


# ---------------------------------------------------------------------------


async def interactive_login(page) -> None:
    """Пошаговый ввод телефона и кода прямо в терминал (docker compose run
    держит stdin/tty, поэтому input() работает как в обычной консоли)."""

    # ------------------------------------------------------ телефон
    phone_input = await find_first(page, PHONE_INPUT, timeout_ms=20_000)
    if phone_input is None:
        if await already_logged_in(page):
            log.info("Профиль уже авторизован — повторный вход не нужен.")
            return
        if await detect_captcha(page):
            await shot(page, "captcha_before_phone")
            raise RuntimeError(
                "На экране входа графическая капча/QR в headless не проходится. "
                "Используйте --manual + VNC (см. README)."
            )
        await shot(page, "no_phone_input")
        raise RuntimeError("Не нашёл поле ввода телефона — обновите PHONE_INPUT в selectors.py")

    while True:
        raw = input("Номер телефона MAX (например +79991234567): ").strip()
        try:
            phone = normalize_phone(raw)
            break
        except ValueError as exc:
            print(f"  ⚠ {exc}")
    await phone_input.click()
    await phone_input.fill("")
    await phone_input.press_sequentially(phone, delay=110)
    await jitter(0.6, 1.4)
    await shot(page, "phone_entered")

    submit = await find_first(page, PHONE_SUBMIT_BUTTON, timeout_ms=5_000)
    if submit is not None:
        await submit.click()
    else:
        await page.keyboard.press("Enter")
    log.info("Номер отправлен, жду экран кода…")

    # ------------------------------------------------------ код
    code_screen = await find_first(page, SMS_INPUT, timeout_ms=90_000)
    if code_screen is None:
        if await detect_captcha(page):
            await shot(page, "captcha_before_code")
        else:
            await shot(page, "no_code_input")
        raise RuntimeError(
            "Экран ввода СМС-кода не появился за 90 c. Сделайте docker compose "
            "logs bridge и посмотрите auth_*.png в ./data/logs/"
        )

    # ВАЖНО: у MAX поле кода может быть «невидимым» — печатаем в фокус.
    await code_screen.click()
    await jitter(0.3, 0.6)
    code = input("Код из СМС: ").strip()
    await page.keyboard.type(code, delay=140)
    await shot(page, "code_entered")

    submit = await find_first(page, SMS_SUBMIT_BUTTON, timeout_ms=3_000)
    if submit is not None:
        try:
            await submit.click()
        except Exception:
            await page.keyboard.press("Enter")
    else:
        await page.keyboard.press("Enter")

    log.info("Код отправлен, жду финальный редирект…")


async def wait_logged_in(page, timeout_s: int = 90) -> None:
    marker = await find_first(page, LOGGED_IN_MARKERS, timeout_ms=timeout_s * 1000)
    if marker is None:
        await shot(page, "login_failed")
        raise RuntimeError(
            "После ввода кода интерфейс мессенджера не появился. Скорее всего "
            "MAX попросил дополнительное подтверждение — посмотрите auth_login_failed.png "
            "и воспользуйтесь --manual/VNC."
        )


async def do_manual_session(cfg: Config, browser: BrowserManager, minutes: int) -> None:
    """Ручной режим: браузер открыт, вы управляетете им через VNC/X11 или по
    подсказкам скриншотов; в терминале по очереди жмём Enter."""
    page = browser.page
    print("=" * 62)
    print(" РЕЖИМ --manual: браузер открыт. Состояние страницы:")
    print("=" * 62)
    await shot(page, "manual_start")
    print(f"  URL страницы: {page.url}")
    print(f"  Скриншот    : {_ts_name('manual')}.png (файл в ./data/logs/)")
    input("\n>>> Когда откроете web.max.ru и видите ЭКРАН ВХОДА — нажмите Enter ")

    # Периодически печатаем состояние, чтобы можно было дебажить со скриншотов
    stop = asyncio.Event()

    async def refresher() -> None:
        while not stop.is_set():
            await asyncio.sleep(20)
            await shot(page, "manual_state")
            print(f"     …скриншот состояния обновлён: {page.url}")

    t = asyncio.create_task(refresher())
    try:
        input(">>> Когда ВВЕДЁТЕ ТЕЛЕФОН и увидите поле кода — нажмите Enter ")
        code = input(">>> Код из СМС (можно Enter, если вводили в браузере): ").strip()
        if code:
            await page.keyboard.type(code, delay=140)
            await page.keyboard.press("Enter")
        input(">>> После успешного входа в мессенджер — нажмите Enter ")
    finally:
        stop.set()
        t.cancel()


async def amain() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.auth",
        description="Интерактивная headless-авторизация в web.max.ru",
    )
    parser.add_argument("--manual", action="store_true",
                        help="вести себя как «я всё делаю руками через VNC/скриншоты»")
    parser.add_argument("--manual-minutes", type=int, default=10,
                        help="для --manual: сколько раз напечатать состояние (каждые 20 c)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(require_tg=False)

    print("\n┌─ Первичная авторизация MAX ─────────────────────────────")
    print("│  Профиль браузера сохраняется в", cfg.profile_dir)
    print("│  Сессия (cookies/IndexedDB) останется активной после выхода")
    print("└──────────────────────────────────────────────────────────\n")

    async with BrowserManager(cfg) as browser:
        page = browser.page
        await page.goto(cfg.max_url, wait_until="domcontentloaded", timeout=90_000)
        await jitter(2.0, 3.5)

        if args.manual:
            await do_manual_session(cfg, browser, args.manual_minutes)
        else:
            if await already_logged_in(page):
                log.info("Мы уже в аккаунте — шаг ввода телефона пропускаю.")
            else:
                await interactive_login(page)
                await wait_logged_in(page)

        # Открываем целевой чат — это и проверка, и «прогрев» индексов.
        max_web = MaxWeb(page, cfg)
        try:
            await max_web.open_target_chat()
        except Exception as exc:  # noqa: BLE001
            await shot(page, "chat_open_failed")
            log.error("Вход есть, но целевой чат не открылся: %s", exc)
            print("\n⚠  Проверьте MAX_CHAT_TITLE / MAX_CHAT_URL и селекторы "
                  "SEARCH_*, CHAT_ITEM в selectors.py (скриншот auth_chat_open_failed.png).")
            return 2

        await shot(page, "success")
        print("\n✅ Авторизация завершена, профиль сохранён.")
        print("   Запускайте основной режим:  docker compose up -d\n")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        print("\nПрервано пользователем")
        sys.exit(130)
