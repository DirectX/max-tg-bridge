"""
app/tg/keyboards.py
===================
Inline-клавиатуры админ-панели. Колбэк-данные — компактные строки вида
``<действие>[:<account_id>[:<доп.>]]``; разбор — в admin_handlers.

Все кнопки создаются ТОЛЬКО через фабрику btn() ниже.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..store import Account


def btn(text: str, callback_data: str) -> InlineKeyboardButton:
    """Единственный способ создавать inline-кнопки в проекте.

    aiogram.types.InlineKeyboardButton — pydantic-модель: принимает ТОЛЬКО
    именованные аргументы (позиционная форма падает с
    TypeError: BaseModel.__init__() takes 1 positional argument but 2 were
    given). Фабрика гарантирует keyword-форму и не даёт такому багу
    повториться.
    """
    return InlineKeyboardButton(text=text, callback_data=callback_data)


STATUS_ICON = {
    "active": "🟢",
    "stopped": "⏸",
    "auth_required": "🔴",
    "authenticating": "🔑",
}


def main_menu(accounts: list[Account]) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        icon = STATUS_ICON.get(acc.status, "❓")
        kb.append([btn(
            f"{icon} {acc.name}", callback_data=f"acc:{acc.id}",
        )])
    kb.append([btn("➕ Добавить аккаунт MAX", callback_data="add")])
    kb.append([btn("📊 Системный статус", callback_data="sys"),
               btn("💾 Бэкап сейчас", callback_data="bak")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def account_actions(acc: Account, listeners_count: int) -> InlineKeyboardMarkup:
    kb = [
        [
            btn("▶️ Запустить", callback_data=f"run:{acc.id}"),
            btn("⏸ Остановить", callback_data=f"stop:{acc.id}"),
        ],
        [
            btn("🔑 Переавторизовать", callback_data=f"relogin:{acc.id}"),
            btn("👤 Владельца сменить", callback_data=f"own:{acc.id}"),
        ],
        [
            btn(f"👥 Слушатели ({listeners_count})",
                                 callback_data=f"lis:{acc.id}"),
            btn("🗑 Удалить", callback_data=f"del:{acc.id}"),
        ],
        # v2.1: инвайт-ссылки
        [
            btn("🔗 Создать инвайт-ссылку", callback_data=f"invnew:{acc.id}"),
            btn("🎟 Активные инвайты", callback_data=f"invlist:{acc.id}"),
        ],
        [btn("⬅️ Меню", callback_data="menu")],
    ]
    # Кнопка «Стоп» не нужна для остановленного, «Запуск» — для активного
    kb[0] = [
        b for b in kb[0]
        if not ((b.callback_data == f"run:{acc.id}" and acc.status == "active")
                or (b.callback_data == f"stop:{acc.id}" and acc.status == "stopped"))
    ] or kb[0]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def listener_line(label: str, uid: int, acc_id: int) -> InlineKeyboardButton:
    """Кнопка-строка слушателя: человекочитаемое имя + @username + id."""
    return btn(f"{label} [{uid}] — 🗑", callback_data=f"lisd:{acc_id}:{uid}")


def listeners_panel(acc_id: int, rows: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """rows: [(tg_user_id, 'Имя Ф. (@user)'), ...] — уже человекочитаемые."""
    kb = [[btn("➕ Добавить слушателя (вручную по ID)",
                                callback_data=f"lisadd:{acc_id}")]]
    for uid, label in rows:
        kb.append([listener_line(label, uid, acc_id)])
    kb.append([btn("⬅️ Назад", callback_data=f"acc:{acc_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_confirm(acc: Account) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        btn(f"✅ Да, удалить «{acc.name}»", callback_data=f"delyes:{acc.id}"),
        btn("❌ Отмена", callback_data=f"acc:{acc.id}"),
    ]])


# ---------------------------------------------------------------- v2.1 invites
def invite_role_picker(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("👀 Для Слушателя (только чтение)",
                              callback_data=f"invrole:{acc_id}:listener")],
        [btn("👑 Для Владельца (чтение+ответы, одноразовая)",
                              callback_data=f"invrole:{acc_id}:owner")],
        [btn("⬅️ Назад", callback_data=f"acc:{acc_id}")],
    ])


def invite_options_picker(acc_id: int, role: str) -> InlineKeyboardMarkup:
    """Параметры для слушателя: 1 раз / 5 / безлимит / 24 часа.
    Для владельца — только подтверждение одноразовой."""
    if role == "owner":
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Создать одноразовую ссылку владельца",
                                  callback_data=f"invopt:{acc_id}:owner:once")],
            [btn("⬅️ Назад", callback_data=f"invnew:{acc_id}")],
        ])
    kb = [
        [btn("1️⃣  Одноразовая (1 человек)",
                              callback_data=f"invopt:{acc_id}:listener:once")],
        [btn("5️⃣  На 5 человек",
                              callback_data=f"invopt:{acc_id}:listener:five")],
        [btn("♾️  Безлимитная",
                              callback_data=f"invopt:{acc_id}:listener:unlim")],
        [btn("⏳  24 часа (одноразовая)",
                              callback_data=f"invopt:{acc_id}:listener:day")],
        [btn("⬅️ Назад", callback_data=f"invnew:{acc_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def invites_panel(acc_id: int, invites: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """invites: [(invite_id, '👀 осталось 2/5 · до 12.11 18:00'), ...]."""
    kb = [[btn("➕ Новая ссылка", callback_data=f"invnew:{acc_id}")]]
    for inv_id, label in invites:
        kb.append([btn(f"{label} — 🚫 отозвать",
                                        callback_data=f"invrev:{acc_id}:{inv_id}")])
    kb.append([btn("⬅️ Назад", callback_data=f"acc:{acc_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def auth_cancel_row() -> list[InlineKeyboardButton]:
    return [btn("✖️ Отменить авторизацию", callback_data="auth_cancel")]


def auth_captcha_row() -> list[InlineKeyboardButton]:
    return [btn("🖼 Капча: инструкция VNC", callback_data="auth_vnc")]


def auth_waited_row() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Повторить код", callback_data="auth_retry_code")],
        auth_cancel_row(),
    ])


def account_picker(accounts: list[Account], prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора аккаунта (для «!макс», когда аккаунтов у владельца несколько)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(a.name, callback_data=f"{prefix}:{a.id}")] for a in accounts
    ])
