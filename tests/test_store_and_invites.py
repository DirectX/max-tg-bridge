"""
tests/test_store_and_invites.py
===============================
Критическая бизнес-логика данных: аккаунты, роли, инвайт-ссылки.
Без браузера, без Telegram, без сети — только SQLite в tmp_path.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest


# =========================================================================
# Аккаунты и роли
# =========================================================================
class TestAccountsAndRoles:
    def test_create_account_and_owner(self, store):
        acc = store.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100)
        assert acc.id > 0 and acc.name == "Школа 5Б"
        assert acc.status == "stopped" and acc.owner_tg_id == 100
        assert store.get_account(acc.id).max_chat_title == "9 Б"
        assert [a.id for a in store.accounts_by_owner(100)] == [acc.id]

    def test_duplicate_name_rejected(self, store):
        store.create_account("Школа 5Б", chat_title="9 Б")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_account("Школа 5Б", chat_title="другой")

    def test_listeners_bind_and_unbind(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        assert store.add_listener(acc.id, 101, "Папа Маши") is True
        assert store.add_listener(acc.id, 101, "дубль") is False   # уже был
        assert store.add_listener(acc.id, 102, "Мама") is True
        assert store.is_listener(acc.id, 101)
        assert not store.is_listener(acc.id, 999)
        assert sorted(r["tg_user_id"] for r in store.list_listeners(acc.id)) == [101, 102]
        assert [a.id for a in store.accounts_by_listener(102)] == [acc.id]
        assert store.remove_listener(acc.id, 101) is True
        assert not store.is_listener(acc.id, 101)

    def test_recipients_owner_plus_listeners(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        store.add_listener(acc.id, 101)
        store.add_listener(acc.id, 102)
        owner, listeners = store.recipients(acc.id)
        assert owner == 100 and sorted(listeners) == [101, 102]

    def test_update_account_whitelist(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        store.update_account(acc.id, status="active", owner_tg_id=200)
        fresh = store.get_account(acc.id)
        assert fresh.status == "active" and fresh.owner_tg_id == 200
        with pytest.raises(ValueError):
            store.update_account(acc.id, hacked_field=1)

    def test_delete_account_cascades(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        store.add_listener(acc.id, 101)
        store.remember_message(acc.id, 100, 5001, "k1", "Учитель", "текст")
        store.mark_seen(acc.id, "k1")
        store.bump_stat(acc.id, "max_to_tg")
        store.delete_account(acc.id)
        assert store.get_account(acc.id) is None
        assert store.list_listeners(acc.id) == []
        assert store.lookup_message(5001) is None
        assert store.get_stats(acc.id) == {}


# =========================================================================
# Инвайты: генерация, лимиты, срок
# =========================================================================
class TestInvites:
    def test_one_time_link(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=1)
        assert inv["max_uses"] == 1 and inv["used_count"] == 0
        assert inv["expires_at"] is None
        assert len(inv["token"]) >= 16 and inv["token"].isalnum() or "-" in inv["token"]

        status, row, account = store.redeem_invite(inv["token"], 201, "Анна", "anna_s")
        assert status == "ok_listener" and account.id == acc.id
        assert row["used_count"] == 1
        # второй переход — уже исчерпан
        assert store.redeem_invite(inv["token"], 202, "Вася", None)[0] == "exhausted"
        assert store.is_listener(acc.id, 201) and not store.is_listener(acc.id, 202)

    def test_multi_use_link(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=5)
        for i in range(5):
            assert store.redeem_invite(inv["token"], 300 + i, f"U{i}", None)[0] == "ok_listener"
        assert store.redeem_invite(inv["token"], 999, "X", None)[0] == "exhausted"
        assert store.get_invite(inv["token"])["used_count"] == 5
        assert len(store.list_listeners(acc.id)) == 5

    def test_unlimited_link(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=0)
        for i in range(30):
            assert store.redeem_invite(inv["token"], 400 + i, f"U{i}", None)[0] == "ok_listener"
        assert store.get_invite(inv["token"])["used_count"] == 30

    def test_expiry(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=3, ttl_seconds=60)
        assert inv["expires_at"] > time.time()
        assert store.redeem_invite(inv["token"], 500, "Свежий", None)[0] == "ok_listener"
        # срок истёк «задним числом»
        store._db.execute("UPDATE invites SET expires_at = ? WHERE id = ?",
                          (int(time.time()) - 1, inv["id"]))
        store._db.commit()
        status, _, _ = store.redeem_invite(inv["token"], 501, "Просроченный", None)
        assert status == "expired"
        assert not store.is_listener(acc.id, 501)
        assert store.get_invite(inv["token"])["used_count"] == 1  # не списался

    def test_invalid_token(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        status, row, account = store.redeem_invite("nope", 600, "X", None)
        assert status == "invalid" and row is None and account is None

    def test_owner_link_is_always_single_use(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "owner", max_uses=99)   # 99 игнорируется
        assert inv["max_uses"] == 1
        status, _, account = store.redeem_invite(inv["token"], 700, "Новый", "nv")
        assert status == "ok_owner"
        # account — снимок ДО применения роли (прежний владелец 100): redeem
        # возвращает аккаунт-источник, не обманывая вызывающего
        assert store.get_account(acc.id).owner_tg_id == 700          # фактически сменился
        # второй — отказ, действующий владелец не перебивается
        assert store.redeem_invite(inv["token"], 701, "Второй", None)[0] == "exhausted"
        assert store.get_account(acc.id).owner_tg_id == 700
        # и владелец не становится слушателем
        assert not store.is_listener(acc.id, 700)

    def test_revoke(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener")
        assert store.revoke_invite(inv["id"]) is True
        assert store.revoke_invite(inv["id"]) is False
        assert store.redeem_invite(inv["token"], 800, "X", None)[0] == "invalid"

    def test_active_only_hides_used_and_expired(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        live = store.create_invite(acc.id, "listener", max_uses=0)
        spent = store.create_invite(acc.id, "listener", max_uses=1)
        expired = store.create_invite(acc.id, "listener", max_uses=1, ttl_seconds=-10)
        store.redeem_invite(spent["token"], 1, "A", None)
        store.redeem_invite(expired["token"], 2, "B", None)
        tokens = {r["token"] for r in store.list_invites(acc.id, active_only=True)}
        assert tokens == {live["token"]}
        assert {r["token"] for r in store.list_invites(acc.id, active_only=False)} \
            == {live["token"], spent["token"], expired["token"]}

    def test_listener_alias_keeps_human_names(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=2)
        store.redeem_invite(inv["token"], 900, "Иван Иванов", "ivanov")
        store.redeem_invite(inv["token"], 901, "Пётр", None)
        rows = {r["tg_user_id"]: r for r in store.list_listeners(acc.id)}
        assert rows[900]["full_name"] == "Иван Иванов"
        assert rows[900]["username"] == "ivanov"
        assert rows[901]["full_name"] == "Пётр"
        assert rows[901]["username"] is None

    def test_manual_add_stays_compatible_with_v2(self, store):
        """Ручное добавление по ID (v2.0) — новые колонки остаются NULL."""
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        store.add_listener(acc.id, 950, "Папа Маши")
        row = [r for r in store.list_listeners(acc.id) if r["tg_user_id"] == 950][0]
        assert row["alias"] == "Папа Маши"
        assert row["username"] is None and row["full_name"] is None
        # инвайт того же человека дозаполняет имя, не дублируя строку
        inv = store.create_invite(acc.id, "listener")
        store.redeem_invite(inv["token"], 950, "Мария П.", "maria_p")
        rows = [r for r in store.list_listeners(acc.id) if r["tg_user_id"] == 950]
        assert len(rows) == 1
        assert rows[0]["full_name"] == "Мария П." and rows[0]["alias"] == "Папа Маши"

    def test_invite_fk_cascade(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        store.create_invite(acc.id, "listener")
        assert store.list_invites(acc.id) != []
        store.delete_account(acc.id)
        assert store.list_invites(acc.id, active_only=False) == []

    def test_invite_cascade_deleted_with_account(self, store):
        """Удаление аккаунта стирает его инвайты: ссылка становится invalid."""
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener")
        token = inv["token"]
        store.delete_account(acc.id)
        assert store.get_invite(token) is None, "инвайт не стёрт каскадом"
        status, row, account = store.redeem_invite(token, 1, "X", None)
        assert status == "invalid" and account is None

    def test_redeem_defensive_branch_no_account(self, store):
        """Defensive-ветка: инвайт указывает на несуществующий аккаунт
        (битый FK вручную, как если бы база была повреждена). Счётчик не
        списывается, роль не применяется."""
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener")
        ghost_id = 999_999
        # новое соединение: PRAGMA foreign_keys по умолчанию OFF — вставка пройдёт
        raw = sqlite3.connect(store._path)
        try:
            raw.execute("UPDATE invites SET account_id = ? WHERE id = ?",
                        (ghost_id, inv["id"]))
            raw.commit()
        finally:
            raw.close()
        status, row, account = store.redeem_invite(inv["token"], 1, "X", None)
        assert status == "no_account" and account is None
        assert store.get_invite(inv["token"])["used_count"] == 0, \
            "счётчик списан на несуществующий аккаунт"
        assert not store.is_listener(acc.id, 1)

    def test_bad_role_rejected(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        with pytest.raises(ValueError):
            store.create_invite(acc.id, "superuser")


# =========================================================================
# Race condition: две корутины по одноразовой ссылке
# =========================================================================
class TestInviteRace:
    @pytest.mark.asyncio
    async def test_two_coroutines_one_time_token(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=1)
        token = inv["token"]

        # обе активации стартуют одновременно (to_thread => реальная параллель)
        results = await asyncio.gather(
            store.redeem_invite_async(token, 1001, "Первый", "p1"),
            store.redeem_invite_async(token, 1002, "Второй", "p2"),
        )
        statuses = [r[0] for r in results]
        assert statuses.count("ok_listener") == 1, statuses
        assert statuses.count("exhausted") == 1, statuses
        assert store.get_invite(token)["used_count"] == 1
        winners = [r for r in results if r[0] == "ok_listener"]
        uid = winners[0][2].id  # не используется; сверяем слушателей ниже
        listeners = [r["tg_user_id"] for r in store.list_listeners(acc.id)]
        assert len(listeners) == 1 and listeners[0] in (1001, 1002)

    @pytest.mark.asyncio
    async def test_many_coroutines_one_time_token(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=1)
        token = inv["token"]
        results = await asyncio.gather(*[
            store.redeem_invite_async(token, 2000 + i, f"U{i}", None)
            for i in range(10)
        ])
        statuses = [r[0] for r in results]
        assert statuses.count("ok_listener") == 1
        assert all(s in ("ok_listener", "exhausted") for s in statuses)
        assert store.get_invite(token)["used_count"] == 1
        assert len(store.list_listeners(acc.id)) == 1

    @pytest.mark.asyncio
    async def test_owner_race_keeps_first_winner(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "owner")
        token = inv["token"]
        results = await asyncio.gather(
            store.redeem_invite_async(token, 3001, "А", None),
            store.redeem_invite_async(token, 3002, "Б", None),
        )
        statuses = [r[0] for r in results]
        assert statuses.count("ok_owner") == 1
        owner = store.get_account(acc.id).owner_tg_id
        winner = [r for r in results if r[0] == "ok_owner"][0]
        assert owner == 3001 or owner == 3002
        assert owner in (3001, 3002)

    @pytest.mark.asyncio
    async def test_multi_use_race_exact_limit(self, store):
        acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100)
        inv = store.create_invite(acc.id, "listener", max_uses=3)
        token = inv["token"]
        results = await asyncio.gather(*[
            store.redeem_invite_async(token, 4000 + i, f"U{i}", None)
            for i in range(8)
        ])
        statuses = [r[0] for r in results]
        assert statuses.count("ok_listener") == 3, statuses
        assert statuses.count("exhausted") == 5, statuses
        assert len(store.list_listeners(acc.id)) == 3


# =========================================================================
# Схема: миграция v2 -> v3 и сохранность данных
# =========================================================================
class TestSchemaMigration:
    def test_v2_database_upgrades_to_v3_without_loss(self, sandbox, cfg):
        from app.store import Store
        db = sandbox["dirs"]["state"] / "v2.db"
        c = sqlite3.connect(db)
        c.executescript("""
          CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, phone TEXT DEFAULT '',
            max_chat_title TEXT NOT NULL DEFAULT '', max_chat_url TEXT NOT NULL DEFAULT '',
            max_self_name TEXT NOT NULL DEFAULT '', owner_tg_id INTEGER,
            status TEXT NOT NULL DEFAULT 'stopped', profile_path TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
          CREATE TABLE listeners (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            tg_user_id INTEGER NOT NULL, alias TEXT DEFAULT '', created_at INTEGER NOT NULL,
            UNIQUE (account_id, tg_user_id));
          CREATE TABLE message_map (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            tg_user_id INTEGER NOT NULL, tg_message_id INTEGER NOT NULL UNIQUE,
            max_message_key TEXT DEFAULT '', max_sender TEXT DEFAULT '',
            max_preview TEXT DEFAULT '', created_at INTEGER NOT NULL);
          CREATE TABLE seen_message (account_id INTEGER NOT NULL, key TEXT NOT NULL,
            first_seen INTEGER NOT NULL, PRIMARY KEY (account_id, key));
          CREATE TABLE stats (account_id INTEGER NOT NULL, name TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account_id, name));
          CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
          PRAGMA user_version = 2;
        """)
        now = int(time.time())
        c.execute("INSERT INTO accounts (name,max_chat_title,owner_tg_id,status,"
                  "created_at,updated_at) VALUES ('Школа 5Б','9 Б',100,'active',?,?)",
                  (now, now))
        aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO listeners (account_id,tg_user_id,alias,created_at)"
                  " VALUES (?,101,'Папа Маши',?)", (aid, now))
        c.execute("INSERT INTO seen_message VALUES (?, 'k-old', ?)", (aid, now))
        c.execute("INSERT INTO message_map (account_id,tg_user_id,tg_message_id,"
                  "max_message_key,max_sender,max_preview,created_at)"
                  " VALUES (?,100,7001,'k-old','Учитель','текст',?)", (aid, now))
        c.commit(); c.close()

        st = Store(db)
        try:
            assert st._version() == 3
            acc = st.get_account(aid)
            assert acc.name == "Школа 5Б" and acc.status == "active"
            row = [r for r in st.list_listeners(aid) if r["tg_user_id"] == 101][0]
            assert row["alias"] == "Папа Маши"
            assert row["username"] is None and row["full_name"] is None
            assert st.mark_seen(aid, "k-old") is False      # дедуп цел
            assert st.lookup_message(7001)["max_preview"] == "текст"
            # invites появились и работают на мигрированной базе
            inv = st.create_invite(aid, "listener")
            assert st.redeem_invite(inv["token"], 102, "Новая", "new")[0] == "ok_listener"
        finally:
            st.close()

        # повторное открытие — идемпотентно
        st2 = Store(db)
        try:
            assert st2._version() == 3 and len(st2.list_listeners(aid)) == 2
        finally:
            st2.close()

    def test_fresh_database_is_v3(self, store):
        assert store._version() == 3
