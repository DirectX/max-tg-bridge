"""
app/tg/auth_fsm.py
==================
Машина состояний интерактивной авторизации MAX через Telegram-бота (2.0).

Сценарий нового аккаунта:
  «Добавить» -> [имя профиля] -> [название чата MAX] -> [tg_id владельца]
  -> (браузер открыт) -> [номер телефона] -> СКРИНШОТ -> [код СМС]
  -> СКРИНШОТ -> (ждём вход) -> открыть чат -> воркер активен.

Сценарий переавторизации: тот же, но имя/чат/владелец уже известны —
стартуем сразу с телефона (профиль-папка сохраняется, нужен лишь новый код).

Каждая веха (после телефона и после кода) присылает админу screenshot
страницы: видно, ушёл ли код или вылезла капча/QR. При капче — подсказка
про VNC и кнопка «повторить код». Вся логика браузера — в AccountManager
(begin_auth/…/auth_finalize); здесь только диалог и валидация ввода.

Порог входа: диалог авторизации ведёт ТОЛЬКО супер-администратор.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..max_dom import MaxAuthError, MaxChatNotFound
from ..store import Store
from ..utils import get_logger, normalize_phone
from ..config import Config
from .bot import TelegramCore
from .format import HTML, bold, code, esc, italic
from .keyboards import auth_cancel_row, auth_captcha_row, auth_waited_row
from .states import AuthFlow

log = get_logger("tg.auth")

CANCEL_HINT = "Отправьте «отмена» или /cancel."
# слова отмены: единый источник правды для предиката и _is_cancel
CANCEL_WORDS = frozenset({"отмена", "отменить", "cancel", "stop", "стоп", "/cancel"})


class AuthController:
    """Точка входа из admin_handlers: запуск сценариев входа из колбэков."""

    def __init__(self) -> None:
        self.start_add_account = None   # async (chat_id, state)
        self.start_relogin = None       # async (chat_id, state, account_id) -> str


def register(router: Router, cfg: Config, store: Store, tg: TelegramCore,
             mgr) -> AuthController:
    label = cfg.tg_ack_prefix
    controller = AuthController()

    def _is_admin(message: Message) -> bool:
        return bool(message.from_user) and message.from_user.id == cfg.admin_tg_id

    async def _fail(message: Message, state: FSMContext, err: str,
                    *, account_id: int | None, is_new: bool) -> None:
        """Сбой шага: сообщаем; сессию НЕ закрываем — можно повторить ввод."""
        await message.answer(f"⚠️ {err}\n{CANCEL_HINT}")

    async def _cancel(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        account_id = data.get("account_id")
        await state.clear()
        if account_id:
            note = await mgr.auth_abort(account_id, "отменено администратором")
            await message.answer(f"{label} {note}")
        else:
            await message.answer(f"{label} Авторизация отменена.")

    async def _screenshot_to_admin(chat_id: int, path, caption: str) -> None:
        await tg.send_screenshot(chat_id, path, caption)

    # ================================================================== NEW
    async def start_add_account(chat_id: int, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AuthFlow.new_name)
        await tg.bot.send_message(
            chat_id,
            f"{label} {bold('Новый аккаунт MAX')}\n\n"
            "1/3. Как назвать профиль?\n"
            f"  Пример: {italic('Школа 5Б')}\n"
            f"  {CANCEL_HINT}", parse_mode=HTML)

    # ============================================================== RELOGIN
    async def start_relogin(chat_id: int, state: FSMContext, account_id: int) -> str:
        """Возвращает 'ok' (запрашиваем телефон) или текст ошибки."""
        await state.clear()
        acc = store.get_account(account_id)
        if acc is None:
            return "Аккаунт не найден."
        await state.update_data(account_id=account_id, is_new=False,
                                chat_title=acc.max_chat_title)
        try:
            state_name = await mgr.begin_auth(account_id, is_new=False)
        except Exception as exc:  # noqa: BLE001
            return f"Не удалось начать вход: {exc}"
        await state.set_state(AuthFlow.phone)
        await chat_prompt_phone(chat_id, state, state_name, relogin=True)
        return "ok"

    async def chat_prompt_phone(chat_id: int, state: FSMContext,
                                home_state: str, *, relogin: bool) -> None:
        if home_state == "logged_in":
            await tg.bot.send_message(
                chat_id,
                f"{label} Профиль уже авторизован (сессия жива) — "
                "телефон и код не нужны. Завершаю вход…",
            )
            await _finish(chat_id, state, logged_in_already=True)
            return
        verb = "Переавторизация" if relogin else "Новый аккаунт"
        await tg.bot.send_message(
            chat_id,
            f"{label} {verb}: браузер открыт, страница входа MAX.\n\n"
            f"2/3. Отправьте {bold('номер телефона')} аккаунта MAX "
            f"(формат +79991234567).\n  {CANCEL_HINT}",
            parse_mode=HTML)

    # --------------------------------------------------------- шаг: ИМЯ (new)
    @router.message(AuthFlow.new_name)
    async def step_name(message: Message, state: FSMContext) -> None:
        if not _is_admin(message):
            return
        if message.text and _is_cancel(message.text):
            return await _cancel(message, state)
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Название слишком короткое. " + CANCEL_HINT)
            return
        if store.get_account_by_name(name):
            await message.answer(f"Название {code(name)} уже занято. Другое?")
            return
        await state.update_data(name=name)
        await state.set_state(AuthFlow.new_chat)
        await message.answer(
            f"2/3. Точное {bold('название чата в MAX')} для этого аккаунта?\n"
            "  Как в списке чатов / шапке. Можно и прямую ссылку (начинается с http).\n"
            f"  {CANCEL_HINT}", parse_mode=HTML)

    # ------------------------------------------------------ шаг: ЧАТ (new)
    @router.message(AuthFlow.new_chat)
    async def step_chat(message: Message, state: FSMContext) -> None:
        if not _is_admin(message):
            return
        if message.text and _is_cancel(message.text):
            return await _cancel(message, state)
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Нужно название чата или ссылка. " + CANCEL_HINT)
            return
        chat_title, chat_url = (("", raw) if raw.startswith("http") else (raw, ""))
        await state.update_data(chat_title=chat_title, chat_url=chat_url)
        await state.set_state(AuthFlow.new_owner)
        await message.answer(
            f"3/3. {bold('Telegram ID Владельца')} — кому прилетают сообщения и кто "
            "может отвечать в MAX?\n"
            "  Целое число (свой /id). «минус» нельзя — только личный user id.\n"
            "  Вы — владелец? Отправьте свой id или 'me'.\n"
            f"  {CANCEL_HINT}", parse_mode=HTML)

    # --------------------------------------------------- шаг: ВЛАДЕЛЕЦ (new)
    @router.message(AuthFlow.new_owner)
    async def step_owner(message: Message, state: FSMContext) -> None:
        if not _is_admin(message):
            return
        if message.text and _is_cancel(message.text):
            return await _cancel(message, state)
        raw = (message.text or "").strip().lower()
        if raw in {"me", "я", "self"}:
            owner_id = message.from_user.id
        elif raw.lstrip("+").isdigit():
            owner_id = int(raw)
        else:
            await message.answer("Нужен числовой Telegram id или 'me'. " + CANCEL_HINT)
            return
        if owner_id <= 0:
            await message.answer("id должен быть положительным (для ЛС). Повторите.")
            return

        data = await state.get_data()
        # Создаём аккаунт (stopped) и сразу стартуем браузер входа.
        try:
            acc = await mgr.create_account(
                data["name"], data.get("chat_title", ""),
                chat_url=data.get("chat_url", ""), owner_tg_id=owner_id)
        except ValueError as exc:
            await state.clear()
            await message.answer(f"⚠️ {exc}")
            return

        await state.update_data(account_id=acc.id, is_new=True)
        try:
            home = await mgr.begin_auth(acc.id, is_new=True)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ Браузер входа не стартовал: {exc}")
            await mgr.delete_account(acc.id)
            await state.clear()
            return
        await state.set_state(AuthFlow.phone)
        await chat_prompt_phone(message.chat.id, state, home, relogin=False)

    # -------------------------------------------------------- шаг: ТЕЛЕФОН
    @router.message(AuthFlow.phone)
    async def step_phone(message: Message, state: FSMContext) -> None:
        if not _is_admin(message):
            return
        if message.text and _is_cancel(message.text):
            return await _cancel(message, state)
        data = await state.get_data()
        account_id = data.get("account_id")
        if not account_id:
            await state.clear()
            await message.answer("Сессия потерялась — начните заново (/admin → Добавить).")
            return
        try:
            phone = normalize_phone(message.text or "")
        except ValueError as exc:
            await message.answer(f"⚠️ {exc}")
            return

        await message.answer(f"{label} Ввожу номер на странице…")
        try:
            shot, captcha = await mgr.auth_submit_phone(account_id, phone)
        except Exception as exc:  # noqa: BLE001
            await _fail(message, state, f"Ввод телефона не удался: {exc}",
                        account_id=account_id, is_new=data.get("is_new", False))
            return

        await _screenshot_to_admin(
            message.chat.id, shot,
            "После ввода номера. Всё ок? Пришлите код из СМС (или 'отмена').")

        if captcha:
            await message.answer(
                f"⚠️ Похоже на капчу/QR — автоввод кода может не сработать.\n"
                "Варианты: попробовать код ещё раз, либо пройти капчу вручную "
                "через VNC (см. README «Авторизация с глазками»).",
                reply_markup=_kb_with([auth_captcha_row(), auth_cancel_row()]))
        else:
            await message.answer(
                f"{label} Пришлите {bold('код из СМС')} (цифры).\n"
                "«повторить» — запросить новый код, «отмена» — свернуть.",
                parse_mode=HTML,
                reply_markup=_kb_with([auth_cancel_row()]))
        await state.update_data(phone=phone)
        await state.set_state(AuthFlow.code)

    # ------------------------------------------------------- шаг: ПОСЛЕ КОДА
    @router.message(AuthFlow.code)
    async def step_code(message: Message, state: FSMContext) -> None:
        if not _is_admin(message):
            return
        data = await state.get_data()
        account_id = data.get("account_id")
        if message.text and _is_cancel(message.text):
            return await _cancel(message, state)

        if not account_id:
            await state.clear()
            await message.answer("Сессия потерялась — начните заново.")
            return

        low = (message.text or "").strip().lower()
        if low in {"повторить", "повтор", "заново", "retry"}:
            await message.answer("Хорошо, жду новый код из СМС.")
            return

        code = "".join(ch for ch in (message.text or "") if ch.isdigit())
        if len(code) < 4:
            await message.answer("Это не похоже на код (мало цифр). Пришлите код или 'отмена'.")
            return

        await message.answer(f"{label} Ввожу код, жду вход…")
        try:
            shot, captcha = await mgr.auth_submit_code(account_id, code)
            await _screenshot_to_admin(message.chat.id, shot, "После ввода кода.")
            logged = await mgr.auth_wait_logged_in(account_id, timeout_s=90)
        except Exception as exc:  # noqa: BLE001
            await _fail(message, state, f"Шаг кода упал: {exc}",
                        account_id=account_id, is_new=data.get("is_new", True))
            return

        if captcha:
            await message.answer(
                "⚠️ После кода видна капча/подтверждение. Пройдите её через VNC "
                "(README) и нажмите «Повторить код», когда ввод готов.",
                reply_markup=auth_waited_row())
            return

        if logged:
            await _finish(message.chat.id, state)
        else:
            await message.answer(
                "⚠️ Судя по странице, вход не завершился. Проверьте скриншот: "
                "код верный? Капча? Нажмите «Повторить код» или «Отменить».",
                reply_markup=auth_waited_row())

    # -------------------------------------------------- завершение (общее)
    async def _finish(chat_id: int, state: FSMContext | None,
                      *, logged_in_already: bool = False) -> None:
        data = await state.get_data() if state else {}
        account_id = data.get("account_id")
        _acc0 = store.get_account(account_id) if account_id else None
        name_hint = _acc0.name if _acc0 else f"#{account_id}"
        if not account_id:
            if state:
                await state.clear()
            return
        try:
            acc = await mgr.auth_finalize(account_id)
        except MaxChatNotFound as exc:
            await tg.notify_admin_html(
                f"⚠️ {bold(name_hint)}: вход есть, но целевой чат не открылся — "
                f"{esc(exc)}\nПроверьте MAX_CHAT_TITLE/URL и селекторы CHAT_ITEM/SEARCH_*."
            )
            return
        except MaxAuthError as exc:
            await message_or_none(tg, chat_id, f"⚠️ Сессия не подтвердилась: {exc}")
            return
        if state:
            await state.clear()
        acc = store.get_account(account_id)
        name = acc.name if acc else f"#{account_id}"
        await tg.bot.send_message(
            chat_id,
            f"✅ Аккаунт {bold(name)} авторизован, воркер запущен"
            + (" (сессия уже была живой)." if logged_in_already else ".")
            + "\nНазначьте слушателей: /admin → выберите аккаунт → «Слушатели».",
            parse_mode=HTML)

    # ------------------------------------------------------------- колбэки
    @router.callback_query(F.data == "auth_vnc")
    async def cb_vnc(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "Как пройти капчу/QR вручную:\n"
            "1) В docker-compose.yml раскомментируй Xvfb/VNC-блок (README, раздел "
            "«Авторизация с глазками»).\n"
            "2) Перезапусти авторизацию, подключись VNC-клиентом через SSH-туннель.\n"
            "3) Пройди капчу мышкой в реальном Chromium.\n"
            "Потом вернись сюда и нажми «Повторить код».")

    @router.callback_query(F.data == "auth_retry_code")
    async def cb_retry(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AuthFlow.code)
        await callback.answer("Жду код")
        await callback.message.answer("Пришлите новый код из СМС (или 'отмена').")

    @router.callback_query(F.data == "auth_cancel")
    async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await _cancel(callback.message, state)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        if _is_admin(message):
            await _cancel(message, state)

    # «отмена» текстом, когда состояние НЕ установлено (защита от залипания).
    # ВАЖНО (aiogram 3.x):
    #   * MagicFilter.in_() принимает ОДИН контейнер, не *args;
    #   * у State нет __invert__ -> «не в AuthFlow» выражается ~StateFilter(...),
    #     который матчит и None-состояние;
    #   * MagicFilter НЕЛЬЗЯ соединять «&» с обычным Filter: композит
    #     «bool & _InvertFilter» падает TypeError при РАЗРЕШЕНИИ (регистрация
    #     проходит молча!). Фильтры передаются позиционными аргументами —
    #     фреймворк сам AND-их;
    #   * F.text может быть None (фото) — предикат через .func с guard.
    @router.message(
        F.text.func(lambda t: bool(t) and t.strip().lower() in CANCEL_WORDS),
        ~StateFilter(AuthFlow.new_name, AuthFlow.new_chat, AuthFlow.new_owner,
                     AuthFlow.phone, AuthFlow.code),
    )
    async def idle_cancel(message: Message, state: FSMContext) -> None:
        if _is_admin(message):
            await _cancel(message, state)

    controller.start_add_account = start_add_account
    controller.start_relogin = start_relogin
    return controller


# ---------------------------------------------------------------------------
def _is_cancel(text: str) -> bool:
    return (text or "").strip().lower() in CANCEL_WORDS


async def message_or_none(tg: TelegramCore, chat_id: int, text: str) -> None:
    try:
        await tg.bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001
        pass


def _kb_with(rows):
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=rows)
