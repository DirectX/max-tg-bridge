"""
tests/test_routing_and_roles.py
================================
Мок-тесты маршрутизации и прав (v2.0/v2.1 бизнес-логика) БЕЗ браузера и БЕЗ
реального Telegram. Два уровня:

1. MAX -> Telegram: AccountWorker._forward_max_to_recipients на живом Store
   + записывающем фейке TG — фан-аут Владельцу и всем Слушателям, разные
   tg_message_id в message_map, изоляция аккаунтов, дедуп/anti-echo.

2. Telegram -> MAX: НАСТОЯЩИЕ хендлеры, зарегистрированные register()
   (user_handlers / admin_handlers), вызываются напрямую с фейковыми
   Message/CallbackQuery. Проверяем: reply Владельца уходит в очередь MAX,
   reply Слушателя блокируется заглушкой «только чтение», /admin и /backup
   от не-админа отклоняются (и не трогают диск).

Также — структурные проверки порядка регистрации: deep_link-/start обязан
стоять ПЕРЕД обычным /start (aiogram матчит хендлеры по порядку).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bridge import AccountWorker
from app.max_dom import MaxImage, MaxMessage

from fakes import (FakeCallback, FakeMgr, FakeMessage, FakeState, FakeTG,
                    build_admin_router, build_user_router, call_callback,
                    handlers_of)


def _worker(cfg, store, tg, account_id):
    return AccountWorker(cfg, store, tg, store.get_account(account_id))


# ===========================================================================
# 1. MAX -> Telegram: фан-аут и изоляция аккаунтов
# ===========================================================================
class TestFanout:
    @pytest.fixture()
    def world(self, cfg, store, tg_recorder):
        a = store.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100,
                                 self_name="Я Админ")
        store.add_listener(a.id, 101, "Мама")
        store.add_listener(a.id, 102, "Папа")
        b = store.create_account("Робо", chat_title="Кружок", owner_tg_id=200)
        store.add_listener(b.id, 201, "Кружок-мама")
        return SimpleNamespace(a=a, b=b, tg=tg_recorder, store=store, cfg=cfg)

    def test_message_goes_to_owner_and_all_listeners(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="k-1", sender="Учитель", text="Собрание в 18:00")))
        assert world.tg.chats_of("text") == [100, 101, 102]
        for chat_id, text in world.tg.texts:
            assert text.startswith("👤 Учитель")
            assert "Собрание в 18:00" in text

    def test_each_recipient_gets_own_tg_message_id(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="k-1", sender="Учитель", text="Собрание в 18:00")))
        # три разных tg_message_id -> три строки message_map, один max_message_key
        ids = [m for m in (1001, 1002, 1003)]
        rows = [world.store.lookup_message(m) for m in ids]
        assert all(r is not None for r in rows), rows
        assert [(r["tg_user_id"], r["account_id"], r["max_message_key"])
                for r in rows] == [(100, world.a.id, "k-1"),
                                   (101, world.a.id, "k-1"),
                                   (102, world.a.id, "k-1")]
        assert len({r["tg_message_id"] for r in rows}) == 3

    def test_isolation_between_accounts(self, world):
        wa = _worker(world.cfg, world.store, world.tg, world.a.id)
        wb = _worker(world.cfg, world.store, world.tg, world.b.id)
        asyncio.run(wa._forward_max_to_recipients(
            MaxMessage(key="a-1", sender="Учитель", text="из школы")))
        asyncio.run(wb._forward_max_to_recipients(
            MaxMessage(key="b-1", sender="Ментор", text="из кружка")))
        assert world.tg.chats_of("text") == [100, 101, 102, 200, 201]
        for chat_id, text in world.tg.texts:
            if chat_id in (200, 201):
                assert "из кружка" in text and "из школы" not in text
            else:
                assert "из школы" in text and "из кружка" not in text
        # ключи дедуплицируются в границах аккаунта: тот же ключ в другом — «новое»
        assert world.store.mark_seen(world.a.id, "b-1") is True
        assert world.store.mark_seen(world.b.id, "a-1") is True

    def test_duplicate_key_not_resent(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        msg = MaxMessage(key="k-dup", sender="Учитель", text="повтор")
        asyncio.run(w._forward_max_to_recipients(msg))
        before = len(world.tg.texts)
        asyncio.run(w._forward_max_to_recipients(msg))
        assert len(world.tg.texts) == before

    def test_mine_and_self_name_are_skipped(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="m-1", sender="Учитель", text="моё mine", mine=True)))
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="m-2", sender="Я Админ", text="эхо по имени")))
        assert world.tg.texts == []

    def test_echo_window_blocks_our_own_reply(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        w._remember_outbound("Спасибо, приняли!")
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="e-1", sender="Учитель", text="Спасибо, приняли!")))
        assert world.tg.texts == []

    def test_photo_message_falls_back_to_stub_text(self, world):
        """Картинку скачать нечем (self.max=None) — чат не должен замолчать."""
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="p-1", sender="Мама", text="",
                       images=[MaxImage(src="blob:https://web.max.ru/x")])))
        assert world.tg.chats_of("text") == [100, 101, 102]
        assert all("изображение недоступно" in t for _, t in world.tg.texts)

    def test_no_recipients_means_silence(self, cfg, store, tg_recorder):
        lonely = store.create_account("Одинокий", chat_title="Чат")
        w = _worker(cfg, store, tg_recorder, lonely.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="n-1", sender="Кто-то", text="привет")))
        assert tg_recorder.texts == [] and tg_recorder.photos == []

    def test_stats_are_persisted_per_account(self, world):
        w = _worker(world.cfg, world.store, world.tg, world.a.id)
        asyncio.run(w._forward_max_to_recipients(
            MaxMessage(key="s-1", sender="Учитель", text="раз")))
        assert world.store.get_stats(world.a.id).get("max_to_tg") == 3
        assert world.store.get_stats(world.b.id) == {}


# ===========================================================================
# 2. Telegram -> MAX: права на ответ
# ===========================================================================
class TestReplyRights:
    @pytest.fixture()
    def setup(self, cfg, store, tg_recorder):
        a = store.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100)
        store.add_listener(a.id, 101, "Мама")
        store.remember_message(a.id, 100, 4001, "k-1", "Учитель", "Собрание в 18:00")
        store.remember_message(a.id, 101, 4002, "k-1", "Учитель", "Собрание в 18:00")
        mgr = FakeMgr()
        router = build_user_router(cfg, store, tg_recorder, mgr)
        return SimpleNamespace(a=a, cfg=cfg, store=store, tg=tg_recorder,
                               mgr=mgr, router=router)

    def test_owner_reply_enters_queue(self, setup):
        msg = FakeMessage(uid=100, text="Будем!",
                          reply_to_message=FakeMessage(message_id=4001))
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert len(setup.mgr.calls) == 1
        account_id, out = setup.mgr.calls[0]
        assert account_id == setup.a.id
        assert out.text == "Будем!"
        assert out.reply_preview == "Собрание в 18:00"
        assert out.owner_chat_id == 100

    def test_listener_reply_blocked_with_readonly_notice(self, setup):
        msg = FakeMessage(uid=101, text="А можно пораньше?",
                          reply_to_message=FakeMessage(message_id=4002))
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == []
        assert setup.tg.acks
        chat_id, note = setup.tg.acks[-1]
        assert chat_id == 101 and "только чтение" in note

    def test_stranger_reply_is_blocked(self, setup):
        msg = FakeMessage(uid=777, text="привет",
                          reply_to_message=FakeMessage(message_id=4001))
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == []
        assert setup.tg.acks and "не владелец" in setup.tg.acks[-1][1]

    def test_reply_to_non_max_message_is_hinted(self, setup):
        msg = FakeMessage(uid=100, text="привет",
                          reply_to_message=FakeMessage(message_id=9999))
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == []
        assert setup.tg.acks and "не на сообщение из MAX" in setup.tg.acks[-1][1]

    def test_owner_prefix_send_single_account(self, setup):
        msg = FakeMessage(uid=100, text="!макс Здравствуйте, а когда собрание?")
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert len(setup.mgr.calls) == 1
        account_id, out = setup.mgr.calls[0]
        assert account_id == setup.a.id
        assert out.text == "Здравствуйте, а когда собрание?"
        assert out.reply_preview is None

    def test_listener_prefix_send_is_blocked(self, setup):
        msg = FakeMessage(uid=101, text="!m привет из школы")
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == []
        assert setup.tg.acks and "только чтение" in setup.tg.acks[-1][1]

    def test_plain_message_without_reply_or_prefix_is_hinted(self, setup):
        msg = FakeMessage(uid=100, text="просто так пишу")
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == []
        assert setup.tg.acks and "Reply" in setup.tg.acks[-1][1]

    def test_bot_messages_are_ignored(self, setup):
        msg = FakeMessage(uid=100, text="!макс хах", is_bot=True)
        asyncio.run(call_callback(setup.router, "on_incoming",
                                  message=msg, state=FakeState()))
        assert setup.mgr.calls == [] and setup.tg.acks == []

    def test_multi_account_owner_chooses_via_picker(self, setup):
        second = setup.store.create_account("Робо", chat_title="Кружок", owner_tg_id=100)
        st = FakeState()
        msg = FakeMessage(uid=100, text="!макс Привет обоим")
        asyncio.run(call_callback(setup.router, "on_incoming", message=msg, state=st))
        assert setup.mgr.calls == [], "при двух аккаунтах отправка откладывается"
        assert st.state is not None, "ожидалось FSM-состояние выбора"
        assert st.data["pending_out"]["text"] == "Привет обоим"

        cb = FakeCallback(f"pick:{second.id}", uid=100)
        asyncio.run(call_callback(setup.router, "cb_pick", event="callback_query",
                                  callback=cb, state=st))
        assert len(setup.mgr.calls) == 1
        account_id, out = setup.mgr.calls[0]
        assert account_id == second.id and out.text == "Привет обоим"
        assert st.cleared == 1

    def test_registration_order_deep_link_before_plain_start(self, setup):
        names = [h.callback.__name__ for h in setup.router.message.handlers]
        assert "cmd_start_invite" in names and "cmd_start" in names
        assert names.index("cmd_start_invite") < names.index("cmd_start"), \
            f"deep_link-/start должен быть раньше обычного: {names}"


# ===========================================================================
# 3. Права администратора
# ===========================================================================
class TestAdminPermissions:
    @pytest.fixture()
    def setup(self, cfg, store, tg_recorder):
        acc = store.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100)
        mgr = FakeMgr()
        router = build_admin_router(cfg, store, tg_recorder, mgr)
        return SimpleNamespace(cfg=cfg, acc=acc, store=store, tg=tg_recorder,
                               mgr=mgr, router=router)

    def test_admin_opens_panel(self, setup):
        msg = FakeMessage(uid=setup.cfg.admin_tg_id, text="/admin")
        asyncio.run(call_callback(setup.router, "cmd_admin", message=msg))
        assert msg.answers
        text, kwargs = msg.answers[-1]
        assert "Панель управления" in text
        kb = kwargs.get("reply_markup")
        buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"acc:{setup.acc.id}" in buttons and "bak" in buttons

    def test_non_admin_admin_command_denied(self, setup):
        msg = FakeMessage(uid=100, text="/admin")
        asyncio.run(call_callback(setup.router, "cmd_admin", message=msg))
        assert msg.last_answer.startswith("⛔")
        assert "Панель управления" not in msg.all_answers_text

    def test_non_admin_backup_denied_and_disk_untouched(self, setup):
        backups = Path(setup.cfg.backups_dir)
        before = sorted(backups.glob("bridge_*")) if backups.exists() else []
        msg = FakeMessage(uid=100, text="/backup")
        asyncio.run(call_callback(setup.router, "cmd_backup", message=msg))
        assert msg.last_answer.startswith("⛔")
        after = sorted(backups.glob("bridge_*")) if backups.exists() else []
        assert after == before, "не-админ создал бэкап на диске"
        assert setup.tg.files == []

    def test_non_admin_callback_denied(self, setup):
        cb = FakeCallback(f"stop:{setup.acc.id}", uid=100)
        asyncio.run(call_callback(setup.router, "cb_stop", event="callback_query",
                                  callback=cb))
        assert cb.message.answers == []
        assert setup.store.get_account(setup.acc.id).status != "stopped" \
            or setup.acc.status == "stopped"   # статус не изменился на «остановлен» принудительно

    def test_non_admin_cannot_touch_invites(self, setup):
        cb = FakeCallback(f"invopt:{setup.acc.id}:listener:once", uid=100)
        asyncio.run(call_callback(setup.router, "cb_invopt", event="callback_query",
                                  callback=cb))
        assert setup.store.list_invites(setup.acc.id, active_only=False) == []

    def test_admin_creates_invite_link(self, setup):
        cb = FakeCallback(f"invopt:{setup.acc.id}:listener:five",
                          uid=setup.cfg.admin_tg_id)
        asyncio.run(call_callback(setup.router, "cb_invopt", event="callback_query",
                                  callback=cb))
        invites = setup.store.list_invites(setup.acc.id)
        assert len(invites) == 1 and invites[0]["max_uses"] == 5
        text, kwargs = cb.message.answers[-1]
        assert "t.me/my_bridge?start=inv_" in text

    def test_admin_backup_button_creates_archive_and_sends(self, setup):
        cb = FakeCallback("bak", uid=setup.cfg.admin_tg_id)
        asyncio.run(call_callback(setup.router, "cb_backup_now", event="callback_query",
                                  callback=cb))
        assert setup.tg.files, "ожидалась доставка архива админу документом"
        chat_id, size, name, caption = setup.tg.files[0]
        assert chat_id == setup.cfg.admin_tg_id
        assert name.endswith(".tar.gz") and size > 0
        assert "Бэкап" in caption and "restore.py" in caption
        # _run_backup: сначала «Собираю бэкап…», затем edit_text с итогом —
        # правка прилетает в ВОЗВРАЩЁННОЕ сообщение (как в реальном aiogram)
        assert "Собираю бэкап" in cb.message.all_answers_text
        assert "Бэкап готов" in cb.message.all_edits_text


# ===========================================================================
# 4. Инвайт-активация через реальный хендлер /start
# ===========================================================================
class TestInviteActivationHandler:
    @pytest.fixture()
    def setup(self, cfg, store, tg_recorder):
        acc = store.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100)
        mgr = FakeMgr()
        router = build_user_router(cfg, store, tg_recorder, mgr)
        inv = store.create_invite(acc.id, "listener", max_uses=2)
        return SimpleNamespace(cfg=cfg, acc=acc, store=store, tg=tg_recorder,
                               mgr=mgr, router=router, inv=inv)

    def _start(self, setup, uid, token, username=None, full="Иван Иванов"):
        msg = FakeMessage(uid=uid, username=username, full_name=full,
                          text=f"/start inv_{token}")
        asyncio.run(call_callback(setup.router, "cmd_start_invite",
                                  message=msg, state=FakeState()))
        return msg

    def test_listener_activation(self, setup):
        msg = self._start(setup, 5001, setup.inv["token"], "petrov", "Пётр С.")
        assert setup.store.is_listener(setup.acc.id, 5001)
        row = [r for r in setup.store.list_listeners(setup.acc.id)
               if r["tg_user_id"] == 5001][0]
        assert row["full_name"] == "Пётр С." and row["username"] == "petrov"
        assert "режиме ЧТЕНИЯ" in msg.last_answer and "Школа 5Б" in msg.last_answer
        notes = [t for t, _ in setup.tg.admin_notes]
        assert any("Пётр С." in t and "@petrov" in t and "Слушателем" in t
                   and "Школа 5Б" in t for t in notes)

    def test_owner_activation(self, setup):
        inv = setup.store.create_invite(setup.acc.id, "owner")
        msg = self._start(setup, 5002, inv["token"], "mama", "Мария В.")
        assert setup.store.get_account(setup.acc.id).owner_tg_id == 5002
        assert "ВЛАДЕЛЕЦ" in msg.last_answer
        assert not setup.store.is_listener(setup.acc.id, 5002)
        assert any("Владельцем" in t for t, _ in setup.tg.admin_notes)

    def test_invalid_token_message(self, setup):
        msg = self._start(setup, 5003, "nope-nope")
        assert "недействительна" in msg.last_answer or "истёк" in msg.last_answer
        assert not setup.store.is_listener(setup.acc.id, 5003)

    def test_exhausted_token_message(self, setup):
        self._start(setup, 5004, setup.inv["token"])
        self._start(setup, 5005, setup.inv["token"])
        msg = self._start(setup, 5006, setup.inv["token"])
        assert "уже использована" in msg.last_answer
        assert not setup.store.is_listener(setup.acc.id, 5006)

    def test_expired_token_message(self, setup):
        inv = setup.store.create_invite(setup.acc.id, "listener", ttl_seconds=-5)
        msg = self._start(setup, 5007, inv["token"])
        assert "недействительна" in msg.last_answer or "истёк" in msg.last_answer
        assert not setup.store.is_listener(setup.acc.id, 5007)

    def test_plain_start_without_payload_lists_roles(self, setup):
        msg = FakeMessage(uid=5001, text="/start")
        asyncio.run(call_callback(setup.router, "cmd_start",
                                  message=msg, state=FakeState()))
        assert "Мост MAX" in msg.last_answer
        assert "не закреплено" in msg.last_answer

    def test_admin_start_points_to_panel_not_orphan(self, setup):
        """Регресс UX-бага: админ с пустой БД видел «вас должен добавить
        администратор» вместо подсказки про /admin."""
        msg = FakeMessage(uid=setup.cfg.admin_tg_id, text="/start")
        asyncio.run(call_callback(setup.router, "cmd_start",
                                  message=msg, state=FakeState()))
        ans = msg.last_answer
        assert "СУПЕР-АДМИНИСТРАТОР" in ans and "/admin" in ans
        assert "не закреплено" not in ans
        assert "Добавить аккаунт MAX" in ans

    def test_two_users_on_one_time_link(self, setup):
        inv = setup.store.create_invite(setup.acc.id, "listener", max_uses=1)
        self._start(setup, 6001, inv["token"])
        assert setup.store.is_listener(setup.acc.id, 6001)
        msg = self._start(setup, 6002, inv["token"])
        assert "уже использована" in msg.last_answer
        assert not setup.store.is_listener(setup.acc.id, 6002)
