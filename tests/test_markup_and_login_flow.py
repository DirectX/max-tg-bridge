"""
tests/test_markup_and_login_flow.py
===================================
Регрессы отчёта 2026-11-16 (рабочая станция + Orange Pi):

1. /admin показывал «🤖 <b>Панель управления</b>» БУКВАЛЬНО: у Bot
   parse_mode=None по умолчанию (защита flat-фан-аута от контента MAX),
   а панели строили HTML — и забывали передать parse_mode в
   answer()/edit_text(). Лечение: модуль app/tg/format.py + явный HTML во
   всех служебных отправках + экранирование динамических фрагментов.

2. Логин падал на «Поле ввода телефона не найдено», потому что web.max.ru
   первым делом рисует QR-экран («Войдите в MAX по QR-коду» / «QR-код
   устарел»), а поле телефона появляется только после клика по
   переключателю «Войти по номеру телефона» (<span class="content
   svelte-13fyxqk">). Лечение: PHONE_SWITCH + _open_phone_form в goto_home.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import selectors as S
from app.tg.format import bold, code, esc, italic


class TestFormatHelpers:
    def test_esc_neutralizes_injection(self):
        hostile = '<b>admin</b> & "quote" <script>alert(1)</script>'
        out = esc(hostile)
        assert "<" not in out and ">" not in out
        assert "&lt;b&gt;" in out and "&amp;" in out

    def test_bold_code_wrap_escaped_content(self):
        assert bold("<i>x</i>") == "<b>&lt;i&gt;x&lt;/i&gt;</b>"
        assert code("a<b") == "<code>a&lt;b</code>"
        assert italic("ок") == "<i>ок</i>"

    def test_helpers_pass_through_plain_text(self):
        assert esc("Школа 5Б") == "Школа 5Б"
        assert bold("Школа 5Б") == "<b>Школа 5Б</b>"


class TestAdminPanelSendsHtml:
    def test_cmd_admin_uses_parse_mode_html(self, cfg, store):
        from fakes import FakeMessage, build_admin_router, call_callback
        router = build_admin_router(cfg, store, SimpleNamespace(), SimpleNamespace())
        msg = FakeMessage(uid=cfg.admin_tg_id, text="/admin")
        asyncio.run(call_callback(router, "cmd_admin", message=msg))
        assert msg.answers, "нет отправки"
        _text, kwargs = msg.answers[-1]
        assert kwargs.get("parse_mode") == "HTML", \
            "cmd_admin отправлен без parse_mode=HTML — теги уйдут скобочками"

    def test_edit_or_send_passes_parse_mode(self):
        """_edit_or_send (все колбэки панелей) обязан передавать parse_mode=HTML
        и иметь flat-фолбэк на битой разметке."""
        import inspect
        from app.tg import admin_handlers
        # находим вложенную функцию register-скоупа по имени
        src = inspect.getsource(admin_handlers.register)
        assert "parse_mode=HTML" in src, "_edit_or_send без HTML"
        assert "parse_mode=None" in src, "нет flat-фолбэка при TelegramBadRequest"

    def test_no_double_wrapped_helpers(self):
        """Регресс исторического бага: '<b>{hbold(x)}</b>' давало двойную
        обёртку; теперь динамика оборачивается ОДНИМ bold()/code() из format."""
        import re
        from pathlib import Path
        pat = re.compile(r"<(b|code|i)>\{\s*(hbold|hcode|bold|code|italic)\(")
        offenders = [f"{f}: {m.group(0)}" for f in Path("app").rglob("*.py")
                     for m in [pat.search(f.read_text(encoding="utf-8"))] if m]
        assert offenders == [], f"двойная обёртка разметки: {offenders}"

    def test_old_markdown_helpers_not_imported(self):
        """aiogram.utils.markdown (hbold/hcode) выведены из употребления:
        весь HTML — через app/tg/format.py."""
        from pathlib import Path
        offenders = [str(f) for f in Path("app").rglob("*.py")
                     if "aiogram.utils.markdown" in f.read_text(encoding="utf-8")]
        assert offenders == [], f"старые h*-хелперы всё ещё импортируются: {offenders}"


class TestQrSwitchLoginFlow:
    """goto_home обязан переключаться с QR-экрана на форму телефона."""

    @pytest.fixture(autouse=True)
    def _no_sleeps(self, monkeypatch):
        """Human-jitter в тестах только тормозит логику — глушим."""
        import app.max_dom as md

        async def _fast(*a, **k):
            return 0.0
        monkeypatch.setattr(md, "jitter", _fast)
        monkeypatch.setattr(md, "human_pause", _fast)

    @staticmethod
    def _mk(cfg, monkeypatch, *, phone_visible, switch_visible, logged_in=False):
        from app.max_dom import AccountTarget, MaxWeb
        tgt = AccountTarget(account_id=1, chat_title="9 Б", tmp_dir=cfg.tmp_dir)
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="https://web.max.ru/")
        page.goto = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"png")
        mw = MaxWeb(page, cfg, tgt)

        async def fake_first(candidates, root=None, timeout_ms=5_000, state="visible"):
            for sel in candidates:
                if sel in S.PHONE_INPUT and phone_visible:
                    return SimpleNamespace(click=AsyncMock(), fill=AsyncMock(),
                                           press_sequentially=AsyncMock())
                if sel in S.PHONE_SWITCH and switch_visible:
                    return SimpleNamespace(click=AsyncMock())
                if sel in S.LOGGED_IN_MARKERS and logged_in:
                    return SimpleNamespace()
            return None
        monkeypatch.setattr(mw, "_first", fake_first)
        return mw

    def test_goto_home_switch_actually_opens_form(self, cfg, monkeypatch):
        """Полный путь: QR-экран → клик → поле телефона появилось."""
        from app.max_dom import AccountTarget, MaxWeb
        tgt = AccountTarget(account_id=1, chat_title="9 Б", tmp_dir=cfg.tmp_dir)
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="https://web.max.ru/")
        page.goto = AsyncMock()
        mw = MaxWeb(page, cfg, tgt)
        state_box = {"phone": False}

        async def fake_first(candidates, root=None, timeout_ms=5_000, state="visible"):
            for sel in candidates:
                if sel in S.PHONE_INPUT and state_box["phone"]:
                    return SimpleNamespace(click=AsyncMock(), fill=AsyncMock(),
                                           press_sequentially=AsyncMock())
                if sel in S.PHONE_SWITCH and not state_box["phone"]:
                    return SimpleNamespace(
                        click=AsyncMock(side_effect=lambda: state_box.__setitem__("phone", True)))
                if sel in S.LOGGED_IN_MARKERS:
                    return None
            return None
        monkeypatch.setattr(mw, "_first", fake_first)
        assert asyncio.run(mw.goto_home()) == "login_form"

    def test_goto_home_immediate_phone_form(self, cfg, monkeypatch):
        mw = self._mk(cfg, monkeypatch, phone_visible=True, switch_visible=False)
        assert asyncio.run(mw.goto_home()) == "login_form"

    def test_goto_home_logged_in_skips_switch(self, cfg, monkeypatch):
        mw = self._mk(cfg, monkeypatch, phone_visible=False, switch_visible=False,
                      logged_in=True)
        assert asyncio.run(mw.goto_home()) == "logged_in"

    def test_goto_home_unknown_without_switch_and_form(self, cfg, monkeypatch):
        mw = self._mk(cfg, monkeypatch, phone_visible=False, switch_visible=False)
        assert asyncio.run(mw.goto_home()) == "unknown"

    @staticmethod
    def _phone_page(cfg, monkeypatch, *, country="7", enabled=True,
                    form_after_switch=False, typed=None):
        """Фейк страницы с формой form.auth--phone как в реальном DOM MAX."""
        from app.max_dom import AccountTarget, MaxWeb
        tgt = AccountTarget(account_id=1, chat_title="9 Б", tmp_dir=cfg.tmp_dir)
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="https://web.max.ru/")
        page.goto = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"png")
        page.keyboard = SimpleNamespace(type=AsyncMock(), press=AsyncMock())
        mw = MaxWeb(page, cfg, tgt)

        box = {"phone": False, "typed": typed if typed is not None else ""}

        class FakeInput:
            async def click(self): pass
            async def fill(self, v): box["typed"] = v
            async def press_sequentially(self, text, delay=0):
                box["typed"] += text
            async def is_enabled(self): return enabled
            async def input_value(self): return box["typed"]

        class FakeBtn:
            async def click(self):
                if form_after_switch:
                    box["phone"] = True

            async def is_enabled(self):
                return enabled

        async def fake_first(candidates, root=None, timeout_ms=5_000, state="visible"):
            for sel in candidates:
                if sel in S.PHONE_INPUT and (box["phone"] or not form_after_switch):
                    return FakeInput()
                if sel in S.PHONE_FORM and (box["phone"] or not form_after_switch):
                    return FakeBtn()
                if sel in S.PHONE_SWITCH and form_after_switch and not box["phone"]:
                    return FakeBtn()
                if sel in S.PHONE_SUBMIT_BUTTON:
                    return FakeBtn()
            return None
        monkeypatch.setattr(mw, "_first", fake_first)
        monkeypatch.setattr(mw, "_country_prefix", AsyncMock(return_value=country))
        monkeypatch.setattr(mw, "detect_captcha", AsyncMock(return_value=False))
        return mw, box

    def test_submit_phone_types_only_national_digits(self, cfg, monkeypatch):
        """Реальный DOM: «+7» живёт в кнопке страны, в поле — только 10 цифр."""
        mw, box = self._phone_page(cfg, monkeypatch, country="7")
        asyncio.run(mw.submit_phone("+7 (999) 123-45-67"))
        assert box["typed"] == "9991234567", box["typed"]

    def test_submit_phone_accepts_8_prefix(self, cfg, monkeypatch):
        mw, box = self._phone_page(cfg, monkeypatch, country="7")
        asyncio.run(mw.submit_phone("8 999 123-45-67"))
        assert box["typed"] == "9991234567"

    def test_submit_phone_country_mismatch_raises(self, cfg, monkeypatch):
        mw, box = self._phone_page(cfg, monkeypatch, country="998")
        with pytest.raises(RuntimeError, match="код"):
            asyncio.run(mw.submit_phone("+79991234567"))

    def test_submit_phone_waits_and_reports_disabled_submit(self, cfg, monkeypatch):
        """Кнопка «Продолжить» стартует disabled; если не активировалась —
        внятная ошибка со скриншотом, а не 30-секундный авто-вей."""
        import app.max_dom as md
        monkeypatch.setattr(md, "SUBMIT_ENABLE_WAIT_S", 0.6)   # тест не спит 12 c
        mw, box = self._phone_page(cfg, monkeypatch, enabled=False)
        with pytest.raises(RuntimeError, match="Продолжить"):
            asyncio.run(mw.submit_phone("+79991234567"))

    def test_submit_phone_retries_switch(self, cfg, monkeypatch):
        """Страница откатилась на QR — submit_phone сам переключается."""
        mw, box = self._phone_page(cfg, monkeypatch, form_after_switch=True)
        shot, captcha = asyncio.run(mw.submit_phone("+79991234567"))
        assert captcha is False
        assert box["typed"] == "9991234567"

    def test_bad_phone_length_raises(self, cfg, monkeypatch):
        mw, box = self._phone_page(cfg, monkeypatch)
        with pytest.raises(RuntimeError, match="Не понимаю номер"):
            asyncio.run(mw.submit_phone("+7123"))

    def test_looks_like_login_detects_qr_screen(self, cfg, monkeypatch):
        mw = self._mk(cfg, monkeypatch, phone_visible=False, switch_visible=True)
        assert asyncio.run(mw._looks_like_login()) is True

    def test_phone_switch_selector_present(self):
        assert hasattr(S, "PHONE_SWITCH") and S.PHONE_SWITCH
        joined = " ".join(S.PHONE_SWITCH).lower()
        assert "номер" in joined


class TestTransportTimings:
    """v2.2.1: бюджеты таймаутов и проба /bot-vs-/file."""

    def test_defaults_and_clamps(self, tmp_path, monkeypatch):
        from app.config import load_config
        env = {
            "TG_BOT_TOKEN": "1:x", "TG_API_BASE": "https://p.example",
            "ADMIN_TG_ID": "9",
            "PROFILES_ROOT": str(tmp_path / "p"), "TRASH_ROOT": str(tmp_path / "t"),
            "TMP_DIR": str(tmp_path / "tm"), "DB_PATH": str(tmp_path / "s" / "db.db"),
            "BROWSER_LOG": str(tmp_path / "l" / "b.log"),
            "SCREENSHOTS_DIR": str(tmp_path / "l"),
            "BACKUPS_DIR": str(tmp_path / "b"),
            # намеренно невалидные: total меньше long-poll, media меньше 30
            "TG_TIMEOUT_TOTAL": "5", "TG_TIMEOUT_MEDIA": "10",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        cfg = load_config()
        assert cfg.tg_timeout_total >= cfg.tg_poll_timeout + 10  # кламп поднимет
        assert cfg.tg_timeout_media >= 30
        assert cfg.tg_send_attempts >= 1

    def test_session_gets_timeout(self, cfg):
        from app.tg.bot import ProxiedAiohttpSession
        s = ProxiedAiohttpSession.create(cfg.tg_api_base, "", timeout=cfg.tg_timeout_total)
        assert float(s.timeout) == float(cfg.tg_timeout_total)

    def test_probe_reuses_transport_session(self):
        """Проба /file обязана идти через сессию транспорта (с TG_PROXY_URL),
        иначе она ходит мимо прокси и ложно reportит «/file не работает»."""
        import inspect
        from app.tg import bot as botmod
        s = inspect.getsource(botmod.TelegramCore._probe_endpoints)
        assert "self.bot.session.create_session()" in s
        assert "aiohttp.ClientSession()" not in s

    def test_extract_photo_respects_flag(self):
        import inspect
        from app.tg import bot as botmod
        s = inspect.getsource(botmod.TelegramCore.extract_photo)
        assert "file_endpoint_available" in s
