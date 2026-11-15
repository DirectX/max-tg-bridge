"""
app/tg/user_handlers.py
=======================
Приём входящих из Telegram в ЛС бота и маршрутизация по ролям (2.0).

Правила:
  1) Reply на сообщение, присланное ботом (есть запись в message_map):
       - Владелец этого аккаунта -> отправка в MAX (reply/цитата);
       - Слушатель  -> вежливая заглушка «только чтение»;
       - посторонний -> заглушка.
  2) Префикс (!макс / !m / /max):
       - Владелец одного аккаунта -> сразу; нескольких -> inline-выбор (FSM);
       - не Владелец -> заглушка.
  3) Всё остальное -> одноразовая (throttled) подсказка.

MAX->TG идёт в ЛС каждому получателю, поэтому ответ — тоже ЛС с ботом.
Контекст (cfg/store/tg/mgr) внедряется через register(), чтобы модуль не
импортировал main (нет циклических зависимостей).
"""

from __future__ import annotations

from dataclasses import asdict

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..bridge import Outgoing
from ..utils import get_logger, truncate
from .keyboards import account_picker
from .states import UserFlow

log = get_logger("tg.user")


def register(router: Router, cfg, store, tg, mgr) -> None:
    label = cfg.tg_ack_prefix

    # ---------------------------------------------------------- приветствие
    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        uid = message.from_user.id if message.from_user else 0
        owned = store.accounts_by_owner(uid)
        listening = store.accounts_by_listener(uid)
        lines = ["🤖 Мост MAX ⇄ Telegram v2.", f"Ваш user id: `{uid}`"]
        if owned:
            lines.append("Вы Владелец: " + ", ".join(a.name for a in owned))
        if listening:
            lines.append("Вы слушаете (только чтение): "
                         + ", ".join(a.name for a in listening))
        if not owned and not listening:
            lines.append("Аккаунтов за вами не закреплено — вас должен добавить "
                         "администратор.")
        if owned:
            lines.append(
                "\nКак отвечать в MAX:\n"
                "• Reply на присланное из MAX сообщение (текст/фото);\n"
                f"• или начать строку с префикса {', '.join(cfg.tg_send_prefixes)}."
            )
        await message.answer("\n".join(lines))

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        await message.answer(f"user_id=`{message.from_user.id}`")

    # -------------------------------------------------- выбор аккаунта (FSM)
    @router.callback_query(UserFlow.pending_choice, F.data.startswith("pick:"))
    async def cb_pick(callback: CallbackQuery, state: FSMContext) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        pending = data.get("pending_out")
        await state.clear()
        if not pending:
            await callback.message.answer(f"{label} срок выбора истёк — повторите отправку.")
            await callback.answer()
            return
        out = Outgoing(**pending)
        ok, note = mgr.submit_outgoing(account_id, out)
        await callback.answer("Выбрано" if ok else "Ошибка")
        if not ok:
            await callback.message.answer(f"{label} {note}")

    @router.callback_query(F.data == "pick_cancel")
    async def cb_pick_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.answer(f"{label} отменено.")

    # ------------------------------------------------------------- входящие
    @router.message(F.chat.type == "private",
                    ~Command("start", "id", "help", "admin", "status"))
    async def on_incoming(message: Message, state: FSMContext) -> None:
        user = message.from_user
        if user is None or user.is_bot:
            return
        uid = user.id
        if uid == cfg.admin_tg_id and message.text and message.text.startswith("/admin"):
            return  # админ-команды ведёт admin_handlers

        text = (message.text or message.caption or "").strip()
        photo = None
        try:
            photo = await tg.extract_photo(message)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось скачать фото из Telegram")
            await tg.throttled_ack(uid, "фото скачать не вышло")
            return

        if not text and photo is None:
            return

        # 1) Reply на сообщение бота
        if message.reply_to_message is not None:
            row = store.lookup_message(message.reply_to_message.message_id)
            if row is not None:
                await _route_reply(row, uid, text, photo, user.full_name)
                return
            await tg.throttled_ack(
                uid, "это ответ не на сообщение из MAX — отвечайте реплеем на "
                     "присланное ботом или используйте префикс")
            return

        # 2) Префикс
        prefix = _match_prefix(text, cfg.tg_send_prefixes)
        if prefix is not None:
            body = text[len(prefix):].strip()
            if not body and photo is None:
                await tg.throttled_ack(uid, "после префикса нужен текст или фото")
                return
            await _route_owner_send(uid, user.full_name, body, photo, state)
            return

        # 3) Прочее
        await tg.throttled_ack(
            uid, "чтобы отправить в MAX — сделайте Reply на сообщение из MAX "
                 "или начните с префикса")

    # ============================================================== internals
    async def _route_reply(row, uid: int, text: str, photo, author: str) -> None:
        account = store.get_account(row["account_id"])
        if account is None:
            await tg.throttled_ack(uid, "этот аккаунт удалён — ответ некуда отправить")
            return
        if account.owner_tg_id != uid:
            if store.is_listener(account.id, uid):
                await tg.throttled_ack(
                    uid, f"у вас режим только чтение по аккаунту «{account.name}»",
                    key=uid)
            else:
                await tg.throttled_ack(uid, "вы не владелец этого аккаунта MAX", key=uid)
            return
        if not text and photo is None:
            await tg.throttled_ack(uid, "пустой ответ проигнорирован")
            return
        out = Outgoing(
            text=text,
            photo_bytes=photo[0] if photo else None,
            photo_name=photo[1] if photo else "tg_photo.jpg",
            reply_preview=row["max_preview"] or None,
            owner_chat_id=uid,
            author_name=author,
        )
        ok, note = mgr.submit_outgoing(account.id, out)
        await tg.throttled_ack(uid, note, window_s=0, key=uid)

    async def _route_owner_send(uid: int, author: str, body: str,
                                photo, state: FSMContext) -> None:
        owned = store.accounts_by_owner(uid)
        if not owned:
            if store.accounts_by_listener(uid):
                await tg.throttled_ack(uid, "у вас режим только чтение — отвечать в MAX "
                                            "может только владелец")
            else:
                await tg.throttled_ack(uid, "за вами нет аккаунтов MAX; добавить вас "
                                            "может админ")
            return
        out = Outgoing(
            text=body,
            photo_bytes=photo[0] if photo else None,
            photo_name=photo[1] if photo else "tg_photo.jpg",
            reply_preview=None,
            owner_chat_id=uid,
            author_name=author,
        )
        if len(owned) == 1:
            ok, note = mgr.submit_outgoing(owned[0].id, out)
            await tg.throttled_ack(uid, note, window_s=0, key=uid)
            return
        # несколько аккаунтов — inline-выбор + черновик в FSM
        from aiogram.types import InlineKeyboardButton
        kb = account_picker(owned, prefix="pick")
        kb.inline_keyboard.append(
            [InlineKeyboardButton("✖️ Отмена", callback_data="pick_cancel")])
        await state.set_state(UserFlow.pending_choice)
        await state.update_data(pending_out=asdict(out))
        await tg.bot.send_message(
            uid, f"{label} В какой аккаунт отправить?\n"
                 f"<{truncate(body or '[фото]', 120)}>", reply_markup=kb)


def _match_prefix(text: str, prefixes) -> str | None:
    low = text.lower()
    for pref in prefixes:
        p = pref.lower()
        if low == p or low.startswith(p + " ") or low.startswith(p + "\n"):
            return pref
    return None
