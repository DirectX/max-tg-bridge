"""
app/auth.py  (v2)
=================
АВАРИЙНЫЙ консольный вход MAX. Штатный путь — через Telegram-бота
(/admin → Добавить / Переавторизовать); CLI нужен, когда Telegram-канал
сам недоступен или отлаживаются селекторы.

Использование:
    python -m app.auth --list
    python -m app.auth --create "Школа 5Б" --chat "9 Б" --owner 123456789
    python -m app.auth --account 3            # перелогин существующего
    python -m app.auth --account 3 --manual   # смотреть скриншоты, шаги Enter'ом

Телефон и код вводятся в терминал; после каждого шага сохраняется
скриншот в /data/logs/ (имена auth_a<id>_*.png).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .account_manager import AccountManager
from .config import load_config
from .max_dom import MaxChatNotFound
from .store import Store
from .tg.bot import TelegramCore
from .utils import get_logger, normalize_phone, setup_logging

log = get_logger("auth")


async def _ask(prompt: str) -> str:
    return (await asyncio.get_running_loop().run_in_executor(
        None, lambda: input(prompt))).strip()


async def _pause() -> None:
    await _ask("— нажмите Enter после того, как всё сделали в браузере … ")


async def run_login(mgr: AccountManager, store: Store, account_id: int,
                    *, manual: bool = False) -> int:
    acc = store.get_account(account_id)
    if acc is None:
        print(f"Аккаунт #{account_id} не найден. --list для списка.")
        return 2

    home = await mgr.begin_auth(account_id, is_new=False)
    sess = mgr.get_auth(account_id)
    assert sess is not None
    maxweb = sess.maxweb

    try:
        if home == "unknown":
            print("Страница не опознана — смотрите скриншот, "
                  "пояс в README «Селекторы».")
            shot = await maxweb.screenshot("unknown_home")
            print(f"Скриншот: {shot}")
            if not manual:
                return 3

        if home != "logged_in":
            while True:
                phone = await _ask("Номер телефона MAX (+79991234567): ")
                try:
                    phone = normalize_phone(phone)
                    break
                except ValueError as exc:
                    print(f"  ⚠ {exc}")
            shot, captcha = await mgr.auth_submit_phone(account_id, phone)
            print(f"Скриншот: {shot}" + ("  [похоже на капчу!]" if captcha else ""))
            if manual:
                await _pause()

            while True:
                code = await _ask("Код из СМС: ")
                code = "".join(c for c in code if c.isdigit())
                if len(code) >= 4:
                    break
                print("  ⚠ минимум 4 цифры")
            shot, captcha = await mgr.auth_submit_code(account_id, code)
            print(f"Скриншот: {shot}" + ("  [похоже на капчу!]" if captcha else ""))
            if manual:
                await _pause()
            if not await mgr.auth_wait_logged_in(account_id, timeout_s=120):
                shot = await maxweb.screenshot("not_logged_in")
                print(f"❌ Вход не подтверждён. Скриншот: {shot}")
                await mgr.auth_abort(account_id, "не подтверждён (CLI)")
                return 4

        try:
            acc = await mgr.auth_finalize(account_id)
        except MaxChatNotFound as exc:
            print(f"⚠️ {exc}\nВход есть, но целевой чат не открылся — "
                  "поправьте название/селекторы и повторите.")
            await mgr.auth_abort(account_id, "чат не открылся")
            return 5

        print(f"\n✅ Готово: аккаунт «{acc.name}» авторизован; "
              f"статус active, воркер будет поднят при следующем старте "
              f"(или: docker compose up -d).")
        return 0
    finally:
        # если сессия ещё жива (вышли по ошибке) — закрываем браузер/откат
        if mgr.get_auth(account_id) is not None:
            await mgr.auth_abort(account_id, "CLI завершился")


async def amain() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth",
                                     description="Консольный вход MAX (v2)")
    parser.add_argument("--list", action="store_true", help="список аккаунтов")
    parser.add_argument("--create", metavar="NAME", help="создать аккаунт")
    parser.add_argument("--chat", default="", help="название чата MAX (для --create)")
    parser.add_argument("--chat-url", default="", help="URL чата MAX (для --create)")
    parser.add_argument("--owner", type=int, default=None, help="tg id владельца")
    parser.add_argument("--account", type=int, help="вход для существующего id")
    parser.add_argument("--manual", action="store_true",
                        help="паузы + скриншоты на каждом шаге (для VNC-отладки)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(require_admin=True)
    store = Store(cfg.db_path)
    tg = TelegramCore(cfg)          # нужен менеджеру для уведомлений; тихий
    mgr = AccountManager(cfg, store, tg)

    try:
        if args.list or (not args.account and not args.create):
            accounts = store.list_accounts()
            if not accounts:
                print("Аккаунтов нет. Создайте: --create \"Имя\" --chat \"чат\" --owner <tg_id>")
            for a in accounts:
                print(f"  #{a.id:<3} {a.status:<14} {a.name:<28} "
                      f"chat={a.max_chat_title or a.max_chat_url or '—'} "
                      f"owner={a.owner_tg_id}")
            return 0

        if args.create:
            acc = await mgr.create_account(args.create, args.chat,
                                           chat_url=args.chat_url,
                                           owner_tg_id=args.owner)
            print(f"Создан аккаунт #{acc.id} «{acc.name}». Профиль: {acc.profile_path}")
            account_id = acc.id
        else:
            account_id = args.account

        return await run_login(mgr, store, account_id, manual=args.manual)
    finally:
        await mgr.shutdown()
        await tg.close()
        store.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        print("\nПрервано")
        sys.exit(130)
