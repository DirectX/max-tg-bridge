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
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message


from ..store import Store
from ..utils import get_logger, read_ram_mb, truncate
from ..config import Config
from .bot import TelegramCore
from .format import HTML, bold, code, esc, italic
from .keyboards import (
    account_actions,
    delete_confirm,
    invite_options_picker,
    invite_role_picker,
    invites_panel,
    listeners_panel,
    main_menu,
)
from .states import AdminInput

log = get_logger("tg.admin")

# opt -> (max_uses, ttl_seconds): 0 = безлимит, None = бессрочно
_INVITE_OPTS: dict[str, tuple[int, int | None]] = {
    "once":  (1, None),
    "five":  (5, None),
    "unlim": (0, None),
    "day":   (1, 24 * 3600),
}

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
        """Правка/отправка сообщения ПАНЕЛИ. Всегда HTML (v2.2.1): раньше
        parse_mode не передавался, а по умолчанию он None -> «<b>Панель
        управления</b>» показывался скобочками. При TelegramBadRequest
        (битая разметка) деградируем в flat, но сообщение не теряем."""
        from aiogram.exceptions import TelegramBadRequest
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=HTML)
        except TelegramBadRequest as exc:
            log.warning("HTML-правка не прошла (%s) — повторяю без разметки", exc)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=None)
        except Exception:  # noqa: BLE001  (message is not modified и пр.)
            try:
                await callback.message.answer(text, reply_markup=kb, parse_mode=HTML)
            except Exception:  # noqa: BLE001
                await callback.message.answer(text, reply_markup=kb, parse_mode=None)

    # ================================================================ /admin
    # ОДИН Command c несколькими именами — это и есть «или»: aiogram
    # объединяет позиционные фильтры через AND, а Command("admin") | Command(...)
    # не поддерживается (у Filter нет __or__). Оба алиаса матчатся здесь.
    @router.message(Command("admin", "status"))
    async def cmd_admin(message: Message) -> None:
        if not _admin(message=message):
            await message.answer("⛔ Доступ только для супер-администратора.")
            return
        accounts = store.list_accounts()
        await message.answer(
            f"{cfg.tg_ack_prefix} {bold('Панель управления')} "
            f"(аккаунтов: {len(accounts)}, в работе: "
            f"{sum(1 for a in accounts if a.status == 'active')})",
            reply_markup=main_menu(accounts), parse_mode=HTML,
        )

    # ------------------------------------------------------------- главное
    @router.callback_query(F.data == "menu")
    async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await state.clear()
        await _edit_or_send(callback,
                            f"{cfg.tg_ack_prefix} {bold('Панель управления')}",
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

    # ---------------------------------------------------- v2.2 бэкапы
    @router.callback_query(F.data == "bak")
    async def cb_backup_now(callback: CallbackQuery) -> None:
        await callback.answer("Бэкап запущен")
        if not _admin(callback=callback):
            return
        await _run_backup(callback.message, callback.from_user.id)

    @router.message(Command("backup"))
    async def cmd_backup(message: Message) -> None:
        if not _admin(message=message):
            await message.answer("⛔ Доступ только для супер-администратора.")
            return
        await _run_backup(message, message.from_user.id)

    async def _run_backup(message: Message, chat_id: int) -> None:
        """Общий путь кнопки и команды: тяжёлая часть вне event loop."""
        from ..backup import backup_now

        note = await message.answer(f"{cfg.tg_ack_prefix} 💾 Собираю бэкап…")
        try:
            res = await backup_now(cfg, store, tg)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ручной бэкап упал")
            await note.edit_text(f"⛔ Бэкап не удался: {exc}")
            return
        if not res.ok:
            await note.edit_text(f"⛔ Бэкап не удался: {res.warning}")
            return
        m = res.manifest
        profiles = ", ".join(m.get("profiles", [])) or "—"
        text = (
            f"💾 Бэкап готов: {code(res.path.name)}\n"
            f"Размер: {res.human_size()}" + (" (AES-256)" if res.encrypted else "") + "\n"
            f"Аккаунтов: {m.get('accounts_active', 0)} активных из "
            f"{m.get('accounts_total', 0)}, слушателей: {m.get('listeners', 0)}\n"
            f"Профили: {esc(profiles)}\n"
            + ("📤 Архив отправлен вам документом выше.\n" if res.sent else
               f"📁 Только на диске: {code(res.path)}\n")
            + (f"⚠ {esc(res.warning)}\n" if res.warning else "")
            + f"Восстановление: {code('scripts/restore.py ' + res.path.name + (' --password …' if res.encrypted else '') + ' --apply')}"
        )
        await note.edit_text(text, parse_mode=HTML)


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
            f"⚠️ Удалить аккаунт {bold(acc.name)}?\n"
            "Воркер будет остановлен, запись и маппинги — стёрты.\n"
            f"Профиль браузера переедет в {code(str(cfg.trash_root))}/ "
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
            f"Новый Владелец для {bold(acc.name)}?\n"
            f"Сейчас: {code(acc.owner_tg_id or '—')}\n"
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
        await message.answer(f"✅ Владелец {bold(acc.name)} → {code(owner_id)}.",
                             parse_mode=HTML)
        try:
            await tg.bot.send_message(
                owner_id,
                f"👑 Вы назначены Владельцем аккаунта MAX «{acc.name}».\n"
                "Отправьте /start, чтобы ознакомиться с режимом работы.")
        except Exception:  # noqa: BLE001
            await message.answer("⚠️ Новому владельцу не отправлено уведомление "
                                 "(он не запускал бота).")

    # -------------------------------------------------------------- слушатели
    def _listener_pairs(account_id: int) -> list[tuple[int, str]]:
        """[(tg_user_id, «Имя Ф. (@user)»)] — человекочитаемо, с фолбэком на alias/id."""
        pairs: list[tuple[int, str]] = []
        for r in store.list_listeners(account_id):
            uid = r["tg_user_id"]
            name = r["full_name"] or r["alias"] or ""
            uname = f" (@{r['username']})" if r["username"] else ""
            label = f"👤 {name}{uname}".strip() if name else f"👤 id {uid}"
            pairs.append((uid, label))
        return pairs

    def _listener_listing(account_id: int) -> str:
        rows = store.list_listeners(account_id)
        if not rows:
            return "  (пока никого)"
        lines = []
        for r in rows:
            uid = r["tg_user_id"]
            name = r["full_name"] or r["alias"]
            uname = f" (@{r['username']})" if r["username"] else ""
            who = f"{name}{uname}" if name else "имя неизвестно"
            lines.append(f"  • {esc(who)} [{code(uid)}]")
        return "\n".join(lines)

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
        text = (f"👥 Слушатели {bold(acc.name)} (только чтение):\n"
                f"{_listener_listing(account_id)}\n\n"
                "Слушатели получают копию всех входящих из MAX, но отвечать не могут.\n"
                "Быстрее подключать людей — инвайт-ссылкой (🔗 в меню аккаунта).")
        await _edit_or_send(callback, text,
                            listeners_panel(account_id, _listener_pairs(account_id)))

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
            f"Формат: {code('123456789 Папа Маши')} (id + опциональный алиас),\n"
            "или 'me' — добавить себя.\n'отмена' — назад.",
            parse_mode=HTML)

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
            f"✅ Слушатель {code(uid)} добавлен к {bold(acc.name)}."
            if added else f"Слушатель {code(uid)} уже был в списке.",
            parse_mode=HTML)
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
        account_id, uid = int(account_id), int(uid)
        removed = store.remove_listener(account_id, uid)
        acc = store.get_account(account_id)
        if acc:
            if removed:
                # вежливо сообщаем отключённому (требование 1.3, опционально)
                try:
                    await tg.bot.send_message(
                        uid,
                        f"ℹ️ Вы отключены от аккаунта MAX «{acc.name}». "
                        "Сообщения из этого чата больше приходить не будут.")
                except Exception:  # noqa: BLE001
                    log.debug("Уведомление об отключении не ушло пользователю %s", uid)
            await _edit_or_send(
                callback,
                ("✅ Слушатель отключён.\n\n" if removed else "Не найдён.\n\n")
                + f"👥 Слушатели {bold(acc.name)}:\n{_listener_listing(account_id)}",
                listeners_panel(account_id, _listener_pairs(account_id)))

    # ================================================== v2.1 инвайт-ссылки
    @router.callback_query(F.data.startswith("invnew:"))
    async def cb_invnew(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        await state.clear()
        account_id = int(callback.data.split(":")[1])
        if store.get_account(account_id) is None:
            return
        await _edit_or_send(
            callback,
            "🔗 Для кого создаём ссылку?\n"
            "• Слушатель — только чтение (можно одноразово / на 5 / безлимит / 24 ч);\n"
            "• Владелец — строго одноразовая: привязка основного пользователя аккаунта.",
            invite_role_picker(account_id))

    @router.callback_query(F.data.startswith("invrole:"))
    async def cb_invrole(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        _, account_id, role = callback.data.split(":")
        await _edit_or_send(
            callback,
            "Какие параметры у ссылки?",
            invite_options_picker(int(account_id), role))

    @router.callback_query(F.data.startswith("invopt:"))
    async def cb_invopt(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        _, account_id, role, opt = callback.data.split(":")
        account_id, role = int(account_id), role
        pair = _INVITE_OPTS.get(opt)
        if pair is None:
            await callback.message.answer("⚠️ Неизвестный параметр ссылки.")
            return
        max_uses, ttl = pair
        acc = store.get_account(account_id)
        if acc is None:
            return
        row = store.create_invite(account_id, role, max_uses=max_uses, ttl_seconds=ttl)
        link = tg.invite_link(row["token"])
        role_ru = "Владелец" if role == "owner" else "Слушатель"
        limit_ru = ("безлимит" if max_uses == 0
                    else f"{max_uses} активаций")
        exp_ru = (time.strftime("%d.%m %H:%M", time.localtime(row["expires_at"]))
                  if row["expires_at"] else "бессрочно")
        if link:
            await callback.message.answer(
                f"🔗 Ссылка готова (роль: {role_ru}, {limit_ru}, действует до "
                f"{exp_ru}):\n\n{code(link)}\n\n"
                "Перешлите её человеку в Telegram. Он откроет чат с ботом и нажмёт "
                "«Запустить» — привязка произойдёт автоматически, его id искать не нужно.",
                parse_mode=HTML)
        else:
            # у бота нет username — остаётся ручной путь /start inv_<token>
            await callback.message.answer(
                f"🔗 Ссылку собрать не удалось: у бота нет @username.\n"
                f"Токен: {code(row['token'])} — человек может ввести "
                f"{code('/start inv_' + row['token'])} вручную.",
                parse_mode=HTML)

    @router.callback_query(F.data.startswith("invlist:"))
    async def cb_invlist(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        account_id = int(callback.data.split(":")[1])
        acc = store.get_account(account_id)
        if acc is None:
            return
        rows = store.list_invites(account_id)
        pairs = []
        for r in rows:
            left = "∞" if r["max_uses"] == 0 else max(r["max_uses"] - r["used_count"], 0)
            tot = "∞" if r["max_uses"] == 0 else r["max_uses"]
            exp = (time.strftime("%d.%m %H:%M", time.localtime(r["expires_at"]))
                   if r["expires_at"] else "бессрочно")
            role = "👑" if r["role"] == "owner" else "👀"
            pairs.append((r["id"], f"{role} осталось {left}/{tot} · до {exp}"))
        text = (f"🎟 Действующие инвайты {bold(acc.name)}:\n"
                + ("\n".join(f"  • {p[1]} (id {p[0]})" for p in pairs)
                   or "  (нет активных — создайте через «🔗 Создать инвайт-ссылку»)"))
        await _edit_or_send(callback, text, invites_panel(account_id, pairs))

    @router.callback_query(F.data.startswith("invrev:"))
    async def cb_invrev(callback: CallbackQuery) -> None:
        await callback.answer()
        if not _admin(callback=callback):
            return
        _, account_id, invite_id = callback.data.split(":")
        revoked = store.revoke_invite(int(invite_id))
        rows = store.list_invites(int(account_id))
        pairs = []
        for r in rows:
            left = "∞" if r["max_uses"] == 0 else max(r["max_uses"] - r["used_count"], 0)
            tot = "∞" if r["max_uses"] == 0 else r["max_uses"]
            exp = (time.strftime("%d.%m %H:%M", time.localtime(r["expires_at"]))
                   if r["expires_at"] else "бессрочно")
            role = "👑" if r["role"] == "owner" else "👀"
            pairs.append((r["id"], f"{role} осталось {left}/{tot} · до {exp}"))
        await _edit_or_send(
            callback,
            ("🚫 Инвайт отозван.\n\n" if revoked else "Инвайт уже удалён.\n\n")
            + "🎟 Действующие инвайты:\n"
            + ("\n".join(f"  • {p[1]} (id {p[0]})" for p in pairs) or "  (пусто)"),
            invites_panel(int(account_id), pairs))

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
        f"📇 {bold(acc.name)} (id={acc.id})",
        f"Статус: {STATUS_LINE.get(acc.status, acc.status)}"
        + (" · 🔑 идёт вход" if snap and snap["auth"] else ""),
        f"Чат MAX: {esc(truncate(acc.max_chat_title or acc.max_chat_url or '—', 60))}",
        f"Владелец: {code(acc.owner_tg_id or '—')} · "
        f"Слушателей: {len(store.list_listeners(acc.id))}",
    ]
    if acc.phone:
        lines.append(f"Телефон: {code(acc.phone)}")
    if acc.max_self_name:
        lines.append(f"Ник MAX (анти-эхо): {esc(truncate(acc.max_self_name, 40))}")
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
        f"⚙️ {bold('Система')} · аптайм {esc(mgr.uptime_str())}",
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
