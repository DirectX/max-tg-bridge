"""
app/tg/format.py
================
Единая точка оформления сообщений бота (v2.2.1).

Принцип безопасности, заложенный ещё в v1: у Bot **parse_mode по умолчанию =
None**. Это не случайность — текст из MAX (имена отправителей, содержимое
сообщений) приходит из внешнего мира и может содержать `<`, `>`, `&`, звёздочки
и «HTML», которое в режиме parse_mode сломало бы рендер или позволило бы
инъекцию разметки в панелей админа. Фан-аут сообщений MAX остаётся ПЛОСКИМ.

HTML-разметка включается ЯВНО и только для служебных сообщений (админ-панель,
FSM-подсказки, статусы). Правила:

  * любой динамический фрагмент (имя аккаунта, id, путь, токен, текст ошибки)
    оборачивается в bold()/code()/italic() — они экранируют содержимое;
  * статические теги пишутся как есть (<b>, <code>) — они под контролем кода;
  * отправка идёт через TelegramCore.send_html()/edit_html(), которые всегда
    передают parse_mode=HTML, поэтому «забыли тег» не превращается в
    показанных пользователю скобочках.

Исторический баг (отчёт 2026-11-16): /admin показывал
«🤖 <b>Панель управления</b>» буквально — потому что answer() вызывался без
parse_mode, а по умолчанию он None. Починено этим модулем + явными хелперами.
"""

from __future__ import annotations

import html as _html

from aiogram.enums import ParseMode

HTML = ParseMode.HTML


def esc(text: object) -> str:
    """Экранирует произвольный текст для Telegram HTML-режима.

    Никогда не передавайте в HTML-строку «сырое» значение из MAX/ENV/ошибок —
    только через esc()/bold()/code().
    """
    return _html.escape(str(text if text is not None else ""), quote=False)


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def italic(text: object) -> str:
    return f"<i>{esc(text)}</i>"


def dim(text: object) -> str:
    """Telegram-специфично: <span class='tg-spoiler'> — приглушённый текст."""
    return f"<span class='tg-spoiler'>{esc(text)}</span>"


def pre(text: object) -> str:
    return f"<pre>{esc(text)}</pre>"


def link(url: str, title: object) -> str:
    return f'<a href="{esc(url)}">{esc(title)}</a>'


def ul(items: list[str]) -> str:
    """Список «• » без вложенного форматирования (items уже отформатированы)."""
    return "\n".join(f"  • {it}" for it in items) if items else ""
