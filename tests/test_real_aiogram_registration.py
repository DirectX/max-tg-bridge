"""
tests/test_real_aiogram_registration.py
=======================================
Регресс на провал 2026-11-16 (прод, Orange Pi): приложение умерло на старте в
auth_fsm.register — MagicFilter.in_() принимает ОДИН контейнер (не *args), а
у State нет __invert__ (вместо ~State нужен ~StateFilter). Заглушки .verify
такое не видят (PH() проглатывает любые аргументы), поэтому этот файл гоняется
ТОЛЬКО с настоящим aiogram и проверяет:

  1. ВСЕ три роутера (auth/admin/user) регистрируются без исключений;
  2. фильтр idle_cancel корректен семантически: срабатывает на «отмена» вне
     AuthFlow, НЕ срабатывает внутри AuthFlow и на сообщениях без текста;
  3. хендлер-фильтры Command не используют неподдерживаемые операторы (|);
  4. кнопки клавиатур — только keyword-аргументы (pydantic), т.е. реальные
     InlineKeyboardMarkup собираются;
  5. TelegramCore.close() освобождает HTTP-сессию (нет «Unclosed client
     session»), НЕ вызывая Telegram-метод close (logout).
"""

from __future__ import annotations

import asyncio
import gc
import inspect

import pytest
from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, Message

from app.tg import admin_handlers, auth_fsm, user_handlers
from fakes import FakeMgr, FakeTG, build_admin_router  # noqa: F401 (styl)


def _register_all(cfg, store):
    """Полный цепочечный register — ровно как в main.Runtime.run()."""
    auth_router = Router()
    auth = auth_fsm.register(auth_router, cfg, store, FakeTG(cfg.admin_tg_id),
                             FakeMgr())
    admin_router = Router()
    admin_handlers.register(admin_router, cfg, store, FakeTG(cfg.admin_tg_id),
                            FakeMgr(), auth)
    user_router = Router()
    user_handlers.register(user_router, cfg, store, FakeTG(cfg.admin_tg_id),
                           FakeMgr())
    return auth_router, admin_router, user_router, auth


def _code_only(path) -> str:
    """Файл без строк-комментариев: сканеры не должны реагировать на
    документирование запрещённых конструкций в комментариях."""
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))


class TestRealAiogramRegistration:
    def test_all_three_routers_register(self, cfg, store):
        auth_r, admin_r, user_r, ctrl = _register_all(cfg, store)
        assert ctrl.start_add_account and ctrl.start_relogin
        n_auth = len(auth_r.message.handlers) + len(auth_r.callback_query.handlers)
        n_admin = len(admin_r.message.handlers) + len(admin_r.callback_query.handlers)
        n_user = len(user_r.message.handlers) + len(user_r.callback_query.handlers)
        assert n_auth >= 8 and n_admin >= 15 and n_user >= 5, (n_auth, n_admin, n_user)

    def test_no_positional_magic_varargs_in_sources(self):
        """Скан исходников: magic-методы, принимающие ОДИН контейнер, не должны
        вызываться с несколькими позиционными аргументами."""
        import re
        from pathlib import Path
        offenders = []
        one_arg_methods = ("in_", "contains_", "regexp", "startswith", "endswith")
        pat = re.compile(r"\.(%s)\(([^)]*[,\"'])[^)]*\)" % "|".join(one_arg_methods))
        for f in Path("app").rglob("*.py"):
            src = f.read_text(encoding="utf-8")
            for m in pat.finditer(src):
                args = m.group(2)
                # допускаем list/tuple/set-литералы и строки без запятых-аргументов
                stripped = args.strip()
                if stripped.startswith(("[", "(", "{")):
                    continue
                if "," in stripped:
                    offenders.append(f"{f}: {m.group(0)}")
        assert offenders == [], "magic-фильтр с *args — упадёт на старте:\n" + "\n".join(offenders)

    def test_no_inverted_state_filters(self):
        """~State не существует (у State нет __invert__) — только ~StateFilter."""
        import re
        from pathlib import Path
        pat = re.compile(r"~\s*(AuthFlow|AdminInput|UserFlow)\.\w+")
        offenders = [f"{f}: {pat.search(_code_only(f)).group(0)}"
                     for f in Path("app").rglob("*.py")
                     if pat.search(_code_only(f))]
        assert offenders == [], f"~State сломает регистрацию: {offenders}"

    def test_command_filter_has_no_or_operator(self):
        """Command | Command не поддерживается (у Filter нет __or__)."""
        import re
        from pathlib import Path
        pat = re.compile(r"Command\([^)]*\)\s*\|")
        offenders = [str(f) for f in Path("app").rglob("*.py")
                     if pat.search(_code_only(f))]
        assert offenders == [], f"Command|Command — TypeError на старте: {offenders}"

    def test_no_magic_ampersand_filter_composition(self):
        """Регресс 2026-11-16 #2: 'MagicFilter & ~StateFilter' регистрируется
        молча, но падает TypeError при РАЗРЕШЕНИИ каждого входящего
        (bool & _InvertFilter). Фильтры в декораторах — только позиционные."""
        import re
        from pathlib import Path
        pat = re.compile(r"@router\.(?:message|callback_query)\((.*?)\)\s*\n\s*async def",
                         re.S)
        offenders = []
        for f in Path("app").rglob("*.py"):
            for m in pat.finditer(_code_only(f)):
                if "&" in m.group(1):
                    offenders.append(f"{f}: {m.group(1)[:120]}")
        assert offenders == [], (
            "&-композиция фильтров рушится при разрешении (регистрация её не "
            "ловит!) — используйте позиционные аргументы:\n" + "\n".join(offenders))

    def test_all_keyboards_build_with_pydantic(self, cfg, store):
        from app.tg.keyboards import (account_actions, account_picker,
                                      delete_confirm, invite_options_picker,
                                      invite_role_picker, invites_panel,
                                      listeners_panel, main_menu)
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        kbs = [
            main_menu(store.list_accounts()),
            account_actions(acc, 2),
            delete_confirm(acc),
            listeners_panel(acc.id, [(101, "👤 Анна (@a)")]),
            invite_role_picker(acc.id),
            invite_options_picker(acc.id, "listener"),
            invite_options_picker(acc.id, "owner"),
            invites_panel(acc.id, [(5, "👀 2/5")]),
            account_picker(store.list_accounts(), prefix="pick"),
        ]
        for kb in kbs:
            assert kb.inline_keyboard, "пустая клавиатура"
            for row in kb.inline_keyboard:
                for b in row:
                    assert isinstance(b, InlineKeyboardButton)
                    assert b.text and b.callback_data


class TestIdleCancelSemantics:
    """Фильтр «отмена вне AuthFlow» — поведение, а не только регистрация."""

    @pytest.fixture()
    def idle_filter(self, cfg, store):
        auth_r, _, _, _ = _register_all(cfg, store)
        h = [x for x in auth_r.message.handlers
             if x.callback.__name__ == "idle_cancel"][0]
        return h

    @pytest.mark.asyncio
    async def test_handler_params_resolve(self, idle_filter):
        # aiogram резолвит params хендлера — state/message должны быть заявлены
        assert "message" in idle_filter.params or idle_filter.varkw
        assert "state" in idle_filter.params or idle_filter.varkw

    @pytest.mark.asyncio
    async def test_state_filter_semantics(self):
        """~StateFilter(AuthFlow.*) == True вне AuthFlow (включая None)."""
        sf = StateFilter(auth_fsm.AuthFlow.new_name, auth_fsm.AuthFlow.new_chat,
                         auth_fsm.AuthFlow.new_owner, auth_fsm.AuthFlow.phone,
                         auth_fsm.AuthFlow.code)
        inv = ~sf
        for raw, expected in [(None, True),
                              ("AuthFlow:phone", False),
                              ("AuthFlow:code", False),
                              ("AdminInput:set_owner", True),  # вне AuthFlow -> ок
                              ("UserFlow:pending_choice", True)]:
            verdict = await inv(Message.model_construct(message_id=1, chat=None,
                                                        from_user=None, text=None),
                                raw_state=raw)
            assert verdict is expected, f"raw_state={raw}: {verdict} != {expected}"

    @pytest.mark.asyncio
    async def test_filters_resolve_via_handler_check(self, idle_filter):
        """Семантика ЧЕРЕЗ ОФИЦИАЛЬНЫЙ путь фреймворка: HandlerObject.check
        (так же, как TelegramEventObserver.trigger). Проверяет и AND-композицию
        позиционных фильтров, и guard на text=None, и «не в AuthFlow»."""

        class St:
            def __init__(self, s): self._s = s
            async def get_state(self): return self._s
            async def set_state(self, s): self._s = s

        def mk(text):
            return Message.model_construct(message_id=1, chat=None,
                                           from_user=None, text=text)

        cases = [
            # (текст, состояние_в_FSM, ожидается_совпадение)
            ("отмена", None, True),
            ("Отмена", None, True),
            ("CANCEL", None, True),
            (None, None, False),                  # фото/стикер — не матчится
            ("", None, False),
            ("!макс привет", None, False),         # не слово отмены
            ("отмена", "AuthFlow:phone", False),   # внутри AuthFlow — не тут
            ("отмена", "AuthFlow:code", False),
            ("отмена", "AdminInput:set_owner", True),  # вне AuthFlow — ок
        ]
        for text, raw, expected in cases:
            ok, _data = await idle_filter.check(mk(text), state=St(raw),
                                                raw_state=raw)
            assert ok is expected, (
                f"check(text={text!r}, state={raw!r}) -> {ok}, ожидалось {expected}")

class TestSessionCleanup:
    @pytest.mark.asyncio
    async def test_core_close_releases_session_without_api_call(self, cfg, monkeypatch):
        """TelegramCore.close() должен закрывать aiohttp-сессию и НЕ дёргать
        Telegram-метод close (logout) — иначе при недоступном прокси сессия
        остаётся открытой («Unclosed client session» в логах краша)."""
        from app.tg.bot import TelegramCore

        core = TelegramCore(cfg)
        session = core.bot.session

        called = {"logout": False}
        async def fake_call(*a, **k):
            called["logout"] = True
            raise AssertionError("bot.close() должен дёргать session.close, не Telegram-метод")
        core.bot.__call__ = fake_call  # type: ignore[method-assign]

        assert session._session is None          # ленивая — ещё не создана
        await core.close()
        assert session._session is None or session._session.closed
        assert not called["logout"]
        del core
        gc.collect()

    def test_close_source_has_no_bot_close(self):
        """Статическая защита: в TelegramCore.close() не осталось self.bot.close()."""
        import re
        from pathlib import Path
        src = Path("app/tg/bot.py").read_text(encoding="utf-8")
        m = re.search(r"async def close\(self\).*?(?=\n    (?:async )?def |\nclass )",
                      src, re.S)
        assert m, "не нашёл TelegramCore.close"
        body = m.group(0)
        assert "self.bot.close()" not in body, \
            "bot.close() — это Telegram logout, а не освобождение ресурсов"
        assert "session.close()" in body
