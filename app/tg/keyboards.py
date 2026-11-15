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
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu")],
    ]
    # Кнопка «Стоп» не нужна для остановленного, «Запуск» — для активного
    kb[0] = [
        b for b in kb[0]
        if not ((b.callback_data == f"run:{acc.id}" and acc.status == "active")
                or (b.callback_data == f"stop:{acc.id}" and acc.status == "stopped"))
    ] or kb[0]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_confirm(acc: Account) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(f"✅ Да, удалить «{acc.name}»", callback_data=f"delyes:{acc.id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"acc:{acc.id}"),
    ]])


def listeners_panel(acc_id: int, listener_ids: list[int]) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton("➕ Добавить слушателя", callback_data=f"lisadd:{acc_id}")]]
    for uid in listener_ids:
        kb.append([InlineKeyboardButton(f"❌ Удалить {uid}",
                                        callback_data=f"lisd:{acc_id}:{uid}")])
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
