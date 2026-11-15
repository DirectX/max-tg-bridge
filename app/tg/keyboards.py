"""
app/tg/keyboards.py
===================
Inline-клавиатуры админ-панели. Колбэк-данные — компактные строки вида
``<действие>[:<account_id>[:<доп.>]]``; разбор — в admin_handlers.CALLBACKS.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..store import Account

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
        kb.append([InlineKeyboardButton(
            f"{icon} {acc.name}", callback_data=f"acc:{acc.id}",
        )])
    kb.append([InlineKeyboardButton("➕ Добавить аккаунт MAX", callback_data="add")])
    kb.append([InlineKeyboardButton("📊 Системный статус", callback_data="sys")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def account_actions(acc: Account, listeners_count: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("▶️ Запустить", callback_data=f"run:{acc.id}"),
            InlineKeyboardButton("⏸ Остановить", callback_data=f"stop:{acc.id}"),
        ],
        [
            InlineKeyboardButton("🔑 Переавторизовать", callback_data=f"relogin:{acc.id}"),
            InlineKeyboardButton("👤 Владельца сменить", callback_data=f"own:{acc.id}"),
        ],
        [
            InlineKeyboardButton(f"👥 Слушатели ({listeners_count})",
                                 callback_data=f"lis:{acc.id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{acc.id}"),
        ],
        # v2.1: инвайт-ссылки
        [
            InlineKeyboardButton("🔗 Создать инвайт-ссылку", callback_data=f"invnew:{acc.id}"),
            InlineKeyboardButton("🎟 Активные инвайты", callback_data=f"invlist:{acc.id}"),
        ],
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu")],
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
    return InlineKeyboardButton(f"{label} [{uid}] — 🗑", callback_data=f"lisd:{acc_id}:{uid}")


def listeners_panel(acc_id: int, rows: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """rows: [(tg_user_id, 'Имя Ф. (@user)'), ...] — уже человекочитаемые."""
    kb = [[InlineKeyboardButton("➕ Добавить слушателя (вручную по ID)",
                                callback_data=f"lisadd:{acc_id}")]]
    for uid, label in rows:
        kb.append([listener_line(label, uid, acc_id)])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"acc:{acc_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_confirm(acc: Account) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(f"✅ Да, удалить «{acc.name}»", callback_data=f"delyes:{acc.id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"acc:{acc.id}"),
    ]])


# ---------------------------------------------------------------- v2.1 invites
def invite_role_picker(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("👀 Для Слушателя (только чтение)",
                              callback_data=f"invrole:{acc_id}:listener")],
        [InlineKeyboardButton("👑 Для Владельца (чтение+ответы, одноразовая)",
                              callback_data=f"invrole:{acc_id}:owner")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"acc:{acc_id}")],
    ])


def invite_options_picker(acc_id: int, role: str) -> InlineKeyboardMarkup:
    """Параметры для слушателя: 1 раз / 5 / безлимит / 24 часа.
    Для владельца — только подтверждение одноразовой."""
    if role == "owner":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("✅ Создать одноразовую ссылку владельца",
                                  callback_data=f"invopt:{acc_id}:owner:once")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"invnew:{acc_id}")],
        ])
    kb = [
        [InlineKeyboardButton("1️⃣  Одноразовая (1 человек)",
                              callback_data=f"invopt:{acc_id}:listener:once")],
        [InlineKeyboardButton("5️⃣  На 5 человек",
                              callback_data=f"invopt:{acc_id}:listener:five")],
        [InlineKeyboardButton("♾️  Безлимитная",
                              callback_data=f"invopt:{acc_id}:listener:unlim")],
        [InlineKeyboardButton("⏳  24 часа (одноразовая)",
                              callback_data=f"invopt:{acc_id}:listener:day")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"invnew:{acc_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def invites_panel(acc_id: int, invites: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """invites: [(invite_id, '👀 осталось 2/5 · до 12.11 18:00'), ...]."""
    kb = [[InlineKeyboardButton("➕ Новая ссылка", callback_data=f"invnew:{acc_id}")]]
    for inv_id, label in invites:
        kb.append([InlineKeyboardButton(f"{label} — 🚫 отозвать",
                                        callback_data=f"invrev:{acc_id}:{inv_id}")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"acc:{acc_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def auth_cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✖️ Отменить авторизацию", callback_data="auth_cancel")]


def auth_captcha_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("🖼 Капча: инструкция VNC", callback_data="auth_vnc")]


def auth_waited_row() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔄 Повторить код", callback_data="auth_retry_code")],
        auth_cancel_row(),
    ])


def account_picker(accounts: list[Account], prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора аккаунта (для «!макс», когда аккаунтов у владельца несколько)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(a.name, callback_data=f"{prefix}:{a.id}")] for a in accounts
    ])
