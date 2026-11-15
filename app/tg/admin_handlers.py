"""
app/tg/admin_handlers.py
========================
Инлайн-панель супер-администратора (/admin), 2.0.

Структура меню:
    /admin
    ├── ➕ Добавить аккаунт MAX ......... -> цепочка AuthFlow (auth_fsm)
    ├── 📊 Системный статус ............. аптайм, RAM (cgroup), браузеры, счётчики
    └── 🟢/🔴/⏸ <Аккаунт>
         ├── ▶️ Запустить / ⏸ Остановить
         ├── 🔑 Переавторизовать ......... AuthFlow поверх существующего профиля
         ├── 👤 Сменить Владельца ........ AdminInput.set_owner
         ├── 👥 Слушатели (N) ............ список + ➕/❌ (AdminInput.add_listener)
         └── 🗑 Удалить ................. дваconfirm; профиль -> data/profiles/.trash

Все текстовые инпуты админки — через FSM-состояния AdminInput, чтобы не
перехватывать сообщения у пользовательских сценариев.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.markdown import hcode, hbold

from ..store import Store
from ..utils import get_logger, read_ram_mb, truncate
from ..config import Config
from .bot import TelegramCore
from .keyboards import (
    account_actions,
    delete_confirm,
    listeners_panel,
    main_menu,
)
from .states import AdminInput

log = get_logger("tg.admin")

STATUS_LINE = {
    "active": "🟢 работает",
    "stopped": "⏸ остановлен",
    "auth_required": "🔴 требуется авторизация",
    "authenticating": "🔑 идёт авторизация",
}


def register(router: Router, cfg: Config, store: Store, tg: TelegramCore,
             mgr, auth) -> None:

    def _admin(message: Message | None = None,
               callback: CallbackQuery | None = None) -> bool:
        if message is not None and message.from_user:
            return message.from_user.id == cfg.admin_tg_id
        if callback is not None and callback.from_user:
            return callback.from_user.id == cfg.admin_tg_id
        return False

    async def _edit_or_send(callback: CallbackQuery, text: str, kb=None) -> None:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:  # noqa: BLE001  (message is not modified и пр.)
            await callback.message.answer(text, reply_markup=kb)

    # ================================================================ /admin
    @router.message(Command("admin"), Command("status"))
    async def cmd_admin(message: Message) -> None:
        if not _admin(message=message):
            await message.answer("⛔ Доступ только для супер-администратора.")
            return
        accounts = store.list_accounts()
        await message.answer(
            f"{cfg.tg_ack_prefix} <b>Панель управления</b> "
            f"(аккаунтов: {len(accounts)}, в работе: "
            f"{sum(1 for a in accounts if a.status == 'active')})",
            reply_markup=main_menu(accounts),
        )

    # ------------------------------------------------------------- главное
    @router.callback_query(F.data == "menu")
    async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await state.clear()
        await _edit_or_send(callback,
                            f"{cfg.tg_ack_prefix} <b>Панель управления</b>",
                            main_menu(store.list_accounts()))

    # -------------------------------------------------------------- добавить
    @router.callback_query(F.data == "add")
    async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await auth.start_add_account(callback.from_user.id, state)

    # ------------------------------------------------------------ системный
    @router.callback_query(F.data == "sys")
    async def cb_sys(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await _edit_or_send(callback, _render_system(cfg, store, mgr))

    # -------------------------------------------------------------- аккаунт
    @router.callback_query(F.data.startswith("acc:"))
    async def cb_account(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await state.clear()
        acc = store.get_account(int(callback.data.split(":")[1]))
        if acc is None:
            return await _edit_or_send(callback, "Аккаунт не найден (уже удалён?).",
                                       main_menu(store.list_accounts()))
        n_list = len(store.list_listeners(acc.id))
        await _edit_or_send(callback, _render_account(acc, store, mgr),
                            account_actions(acc, n_list))

    @router.callback_query(F.data.startswith("run:"))
    async def cb_run(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        note = await mgr.start_account(int(callback.data.split(":")[1]))
        await _after_action(callback, note)

    @router.callback_query(F.data.startswith("stop:"))
    async def cb_stop(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        note = await mgr.stop_account(int(callback.data.split(":")[1]))
        await _after_action(callback, note)

    @router.callback_query(F.data.startswith("relogin:"))
    async def cb_relogin(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        account_id = int(callback.data.split(":")[1])
        await callback.message.answer(
            f"🔑 Переавторизация «{truncate(store.get_account(account_id).name if store.get_account(account_id) else '?', 40)}»: "
            "поднимаю браузер поверх сохранённого профиля…")

        async def _go() -> None:
            res = await auth.start_relogin(callback.from_user.id, state, account_id)
            if res != "ok":
                await callback.message.answer(f"⚠️ {res}")

        asyncio.create_task(_go())   # браузер поднимается долго — не блокируем колбэк

    @router.callback_query(F.data.startswith("del:"))
    async def cb_del(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        acc = store.get_account(int(callback.data.split(":")[1]))
        if acc is None:
            return
        await _edit_or_send(
            callback,
            f"⚠️ Удалить аккаунт <b>{hbold(acc.name)}</b>?\n"
            "Воркер будет остановлен, запись и маппинги — стёрты.\n"
            f"Профиль браузера переедет в {hcode(str(cfg.trash_root))}/ "
            "(ручное удаление, откат возможен).",
            delete_confirm(acc))

    @router.callback_query(F.data.startswith("delyes:"))
    async def cb_delyes(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        note = await mgr.delete_account(int(callback.data.split(":")[1]))
        await _edit_or_send(callback, f"🗑 {note}", main_menu(store.list_accounts()))

    # ------------------------------------------------------- смена владельца
    @router.callback_query(F.data.startswith("own:"))
    async def cb_own(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        account_id = int(callback.data.split(":")[1])
        acc = store.get_account(account_id)
        if acc is None:
            return
        await state.set_state(AdminInput.set_owner)
        await state.update_data(account_id=account_id)
        await callback.message.answer(
            f"Новый Владелец для «{acc.name}»?\n"
            f"Сейчас: {hcode(str(acc.owner_tg_id or '—'))}\n"
            "Пришлите числовой Telegram id (или 'me'). 'отмена' — оставить как было.")

    @router.message(AdminInput.set_owner)
    async def msg_set_owner(message: Message, state: FSMContext) -> None:
        if not _admin(message=message):
            return
        raw = (message.text or "").strip().lower()
        if raw in {"отмена", "cancel"}:
            await state.clear()
            await message.answer("Оставил владельца без изменений.")
            return
        owner_id = message.from_user.id if raw in {"me", "я"} else \
            (int(raw) if raw.lstrip("+").isdigit() else None)
        if owner_id is None or owner_id <= 0:
            await message.answer("Нужен положительный числовой id или 'me'.")
            return
        data = await state.get_data()
        await state.clear()
        account_id = data.get("account_id", 0)
        acc = store.get_account(account_id)
        if acc is None:
            await message.answer("Аккаунт не найден.")
            return
        store.update_account(account_id, owner_tg_id=owner_id)
        await message.answer(f"✅ Владелец «{acc.name}» → {owner_id}.")
        try:
            await tg.bot.send_message(
                owner_id,
                f"👑 Вы назначены Владельцем аккаунта MAX «{acc.name}».\n"
                "Отправьте /start, чтобы ознакомиться с режимом работы.")
        except Exception:  # noqa: BLE001
            await message.answer("⚠️ Новому владельцу не отправлено уведомление "
                                 "(он не запускал бота).")

    # -------------------------------------------------------------- слушатели
    @router.callback_query(F.data.startswith("lis:"))
    async def cb_lis(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await state.clear()
        account_id = int(callback.data.split(":")[1])
        acc = store.get_account(account_id)
        if acc is None:
            return
        rows = store.list_listeners(account_id)
        ids = [r["tg_user_id"] for r in rows]
        listing = "\n".join(
            f"  • {hcode(str(r['tg_user_id']))} {r['alias'] or ''}" for r in rows
        ) or "  (пока никого)"
        text = (f"👥 Слушатели «{hbold(acc.name)}» (только чтение):\n{listing}\n\n"
                "Слушатели получают копию всех входящих из MAX, но отвечать не могут.")
        await _edit_or_send(callback, text, listeners_panel(account_id, ids))

    @router.callback_query(F.data.startswith("lisadd:"))
    async def cb_lisadd(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        account_id = int(callback.data.split(":")[1])
        await state.set_state(AdminInput.add_listener)
        await state.update_data(account_id=account_id)
        await callback.message.answer(
            "Кого добавить в слушатели?\n"
            "Формат: <code>123456789 Папа Маши</code> (id + опциональный алиас),\n"
            "или 'me' — добавить себя.\n'отмена' — назад.")

    @router.message(AdminInput.add_listener)
    async def msg_add_listener(message: Message, state: FSMContext) -> None:
        if not _admin(message=message):
            return
        raw = (message.text or "").strip()
        if raw.lower() in {"отмена", "cancel"}:
            await state.clear()
            await message.answer("Ок.")
            return
        parts = raw.split(maxsplit=1)
        token = parts[0].lower()
        alias = parts[1].strip() if len(parts) > 1 else ""
        uid = message.from_user.id if token in {"me", "я"} else \
            (int(token) if token.lstrip("+").isdigit() else None)
        if uid is None or uid <= 0:
            await message.answer("Не понял id. Формат: «123456789 Алиас».")
            return
        data = await state.get_data()
        await state.clear()
        account_id = data.get("account_id", 0)
        acc = store.get_account(account_id)
        if acc is None:
            await message.answer("Аккаунт не найден.")
            return
        if acc.owner_tg_id == uid:
            await message.answer("Это и есть Владелец — слушатель не нужен.")
            return
        added = store.add_listener(account_id, uid, alias)
        await message.answer(
            f"✅ Слушатель {uid} добавлен к «{acc.name}»."
            if added else f"Слушатель {uid} уже был в списке.")
        try:
            await tg.bot.send_message(
                uid,
                f"👀 Вы добавлены слушателем аккаунта MAX «{acc.name}»: "
                "сюда будут приходить копии входящих сообщений (текст и фото).\n"
                "Режим — только чтение. /start покажет все ваши аккаунты.")
        except Exception:  # noqa: BLE001
            await message.answer("⚠️ Слушателю не отправлено уведомление — "
                                 "он должен сначала нажать /start боту.")

    @router.callback_query(F.data.startswith("lisd:"))
    async def cb_lisd(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        _, account_id, uid = callback.data.split(":")
        removed = store.remove_listener(int(account_id), int(uid))
        acc = store.get_account(int(account_id))
        rows = store.list_listeners(int(account_id))
        ids = [r["tg_user_id"] for r in rows]
        listing = "\n".join(
            f"  • {hcode(str(r['tg_user_id']))} {r['alias'] or ''}" for r in rows
        ) or "  (пока никого)"
        if acc:
            await _edit_or_send(
                callback,
                ("✅ Удалён.\n\n" if removed else "Не найдён.\n\n")
                + f"👥 Слушатели «{hbold(acc.name)}»:\n{listing}",
                listeners_panel(acc.id, ids))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        if _admin(message=message):
            await message.answer(
                "Я — мост MAX ⇄ Telegram v2.\n"
                "• /admin — панель аккаунтов и прав\n"
                "• /status — краткая сводка\n\n" + _render_system(cfg, store, mgr))
        else:
            await message.answer(
                "Я пересылаю сообщения изMAX в Telegram. /start — ваши аккаунты "
                "и режим (Владелец может отвечать, слушатель — только читать).")


# ===========================================================================
def _render_account(acc, store: Store, mgr) -> str:
    snap = next((s for s in mgr.snapshot() if s["id"] == acc.id), None)
    lines = [
        f"📇 <b>{hbold(acc.name)}</b> (id={acc.id})",
        f"Статус: {STATUS_LINE.get(acc.status, acc.status)}"
        + (" · 🔑 идёт вход" if snap and snap["auth"] else ""),
        f"Чат MAX: {truncate(acc.max_chat_title or acc.max_chat_url or '—', 60)}",
        f"Владелец: {hcode(str(acc.owner_tg_id or '—'))} · "
        f"Слушателей: {len(store.list_listeners(acc.id))}",
    ]
    if acc.phone:
        lines.append(f"Телефон: {hcode(acc.phone)}")
    if acc.max_self_name:
        lines.append(f"Ник MAX (анти-эхо): {truncate(acc.max_self_name, 40)}")
    if snap:
        if snap["stats"]:
            lines.append(f"Живо: {snap['stats']}")
        persisted = snap.get("persisted") or {}
        if persisted and not snap["stats"]:
            lines.append(
                f"Накоплено: MAX→TG {persisted.get('max_to_tg', 0)}, "
                f"фото {persisted.get('images', 0)}, "
                f"TG→MAX {persisted.get('tg_to_max', 0)}")
    return "\n".join(lines)


def _render_system(cfg: Config, store: Store, mgr) -> str:
    used, limit = read_ram_mb()
    snap = mgr.snapshot()
    active = sum(1 for s in snap if s["status"] == "active")
    lines = [
        f"⚙️ <b>Система</b> · аптайм {mgr.uptime_str()}",
        f"RAM: {used or '?'} МБ" + (f" / {limit} МБ" if limit else "") +
        f" · браузеров: {len(mgr.running_ids())}",
        f"Аккаунтов: {len(snap)} (активных {active})",
    ]
    for s in snap:
        icon = {"active": "🟢", "auth_required": "🔴", "stopped": "⏸",
                "authenticating": "🔑"}.get(s["status"], "⚪")
        p = s.get("persisted") or {}
        line = (f"  {icon} {truncate(s['name'], 24)} — MAX→TG {p.get('max_to_tg', 0)}"
                f" (фото {p.get('images', 0)}), TG→MAX {p.get('tg_to_max', 0)}")
        lines.append(line)
    return "\n".join(lines)


async def _after_action(callback: CallbackQuery, note: str) -> None:
    """Результат run/stop: коротким сообщением, панель не перерисовываем —
    статус всё равно меняется с задержкой в воркере."""
    await callback.message.answer(f"ℹ️ {note}")
