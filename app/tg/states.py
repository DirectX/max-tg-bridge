"""
app/tg/states.py
================
FSM-состояния v2 (aiogram.fsm, MemoryStorage — состояние управления
не переживает рестарт, и не должно: авторизацию всегда можно начать заново).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    """Владелец с несколькими аккаунтами: ждём выбор «в какой отправить»."""
    pending_choice = State()


class AuthFlow(StatesGroup):
    """Интерактивный вход MAX через бота (admin_handlers запускает)."""
    new_name = State()      # только для нового аккаунта
    new_chat = State()
    new_owner = State()
    phone = State()         # общий участок (новый аккаунт и переавторизация)
    code = State()


class AdminInput(StatesGroup):
    """Одноточечные текстовые инпуты админки."""
    set_owner = State()     # id нового владельца
    add_listener = State()  # "tg_user_id [алиас]"
