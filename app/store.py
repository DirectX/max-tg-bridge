"""
app/store.py  (v2)
==================
Хранилище ролевой модели и состояния моста. SQLite, стандартный sqlite3.
Схема версионируется через PRAGMA user_version; миграции — функции вида
``_mig_<n>``. Текущая версия: 2.

Версии:
  0 -> 1 : первичная v1-схема (seen_message/msg_map/meta) — историческая
  1 -> 2 : v2 Multi-Account & Role-Based:
           accounts, listeners, message_map; seen_message с account_id.
           Данные v1 (один общий мост) при наличии мигрируются в «аккаунт №1»
           с seeding'ом из ENV (см. seed_v1).

Все записи — короткие транзакции в WAL; вызовы синхронные (микросекунды,
отдельный пул потоков не нужен при 2-5 аккаунтах и школьных объёмах).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger("store")

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Схема v2
# ---------------------------------------------------------------------------
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,           -- «Школа 5Б»
    phone           TEXT    DEFAULT '',                -- для справки админу
    max_chat_title  TEXT    NOT NULL DEFAULT '',       -- чат MAX для чтения
    max_chat_url    TEXT    NOT NULL DEFAULT '',       -- опц. прямой URL чата
    max_self_name   TEXT    NOT NULL DEFAULT '',       -- ник владельца MAX (анти-эхо)
    owner_tg_id     INTEGER,                           -- Владелец (1-к-1), NULL = не назначен
    status          TEXT    NOT NULL DEFAULT 'stopped',-- active|stopped|auth_required|authenticating
    profile_path    TEXT    NOT NULL DEFAULT '',       -- /data/profiles/account_<id>
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listeners (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tg_user_id  INTEGER NOT NULL,
    alias       TEXT    DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE (account_id, tg_user_id)
);

CREATE TABLE IF NOT EXISTS message_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tg_user_id      INTEGER NOT NULL,   -- КОМУ ушло (у каждого свой tg_message_id!)
    tg_message_id   INTEGER NOT NULL UNIQUE,
    max_message_key TEXT    DEFAULT '',
    max_sender      TEXT    DEFAULT '',
    max_preview     TEXT    DEFAULT '', -- для цитаты при ответе Владельца
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mm_account ON message_map(account_id);
CREATE INDEX IF NOT EXISTS idx_mm_user   ON message_map(account_id, tg_user_id);

CREATE TABLE IF NOT EXISTS seen_message (
    account_id INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (account_id, key)
);

CREATE TABLE IF NOT EXISTS stats (
    account_id INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    value      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, name)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


# ---------------------------------------------------------------------------
# Доменная модель
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Account:
    id: int
    name: str
    phone: str
    max_chat_title: str
    max_chat_url: str
    max_self_name: str
    owner_tg_id: int | None
    status: str
    profile_path: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Account":
        return cls(
            id=row["id"], name=row["name"], phone=row["phone"],
            max_chat_title=row["max_chat_title"], max_chat_url=row["max_chat_url"],
            max_self_name=row["max_self_name"], owner_tg_id=row["owner_tg_id"],
            status=row["status"], profile_path=row["profile_path"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @property
    def is_active(self) -> bool:
        return self.status == "active"


# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self._path = path
        self._retention_days = retention_days
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        self._db.execute("PRAGMA foreign_keys=ON;")
        self._migrate()
        self._purge_old()

    # ================================================================ schema
    def _version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _set_version(self, v: int) -> None:
        self._db.execute(f"PRAGMA user_version={v}")
        self._db.commit()

    def _table_exists(self, name: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _migrate(self) -> None:
        with self._lock:
            ver = self._version()
            if ver == 0:
                if self._table_exists("seen_message") and not self._table_exists("accounts"):
                    log.info("Найдена v1-база — мигрируем в схему v2")
                    self._migrate_v1_to_v2()
                else:
                    self._db.executescript(_SCHEMA_V2)
                    log.info("Создана свежая v2-база")
                self._set_version(SCHEMA_VERSION)
            elif 0 < ver < SCHEMA_VERSION:
                self._db.executescript(_SCHEMA_V2)
                self._set_version(SCHEMA_VERSION)
            # проверка целостности минимальная
            missing = [t for t in ("accounts", "listeners", "message_map", "seen_message")
                       if not self._table_exists(t)]
            if missing:
                raise RuntimeError(f"БД повреждена: отсутствуют таблицы {missing}")

    def _migrate_v1_to_v2(self) -> None:
        """v1: seen_message(key TEXT PK), msg_map(tg_message_id...), meta.
        Создаём v2-поверхность и переносим данные в «аккаунт-наследник»."""
        old_seen = [r["key"] for r in self._db.execute("SELECT key FROM seen_message")]
        old_map = self._db.execute("SELECT * FROM msg_map").fetchall()
        now = int(time.time())

        # seen_message v1 имеет ДРУГУЮ структуру (key TEXT PK без account_id) —
        # CREATE TABLE IF NOT EXISTS её не починит: сносим после чтения данных.
        self._db.execute("DROP TABLE seen_message")
        self._db.execute("DROP TABLE msg_map")
        self._db.executescript(_SCHEMA_V2)

        # временный флаг: реальный посев (название/чат/владелец) придёт из ENV
        self._db.execute(
            "INSERT INTO accounts (name, phone, max_chat_title, max_chat_url, max_self_name,"
            " owner_tg_id, status, profile_path, created_at, updated_at) "
            "VALUES ('(миграция v1: заполните в /admin)', '', '', '', '', NULL, 'stopped',"
            " '', ?, ?)",
            (now, now),
        )
        acc_id = int(self._db.execute("SELECT last_insert_rowid()").fetchone()[0])
        for key in old_seen:
            self._db.execute(
                "INSERT OR IGNORE INTO seen_message (account_id, key, first_seen) VALUES (?,?,?)",
                (acc_id, key, now),
            )
        for row in old_map:
            # created_at из v1 мог быть любым — не даём _purge_old() съесть
            # только что перенесённые строки: свежая дата безопаснее честной.
            self._db.execute(
                "INSERT OR IGNORE INTO message_map (account_id, tg_user_id, tg_message_id,"
                " max_message_key, max_sender, max_preview, created_at)"
                " VALUES (?,0,?,?,?,?,?)",
                (acc_id, row["tg_message_id"], row["max_node_key"] or "",
                 row["max_sender"] or "", row["max_preview"] or "", now),
            )
        self._db.execute("DROP TABLE IF EXISTS msg_map")
        self._db.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES ('migrated_from_v1', ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        self._db.commit()
        log.info("v1 -> v2: аккаунт id=%d, seen=%d, map=%d", acc_id, len(old_seen), len(old_map))

    def seed_migrated_account(
        self, account_id: int, *, name: str, chat_title: str, owner_tg_id: int | None,
        profile_path: str, self_name: str = "",
    ) -> None:
        """Дозаполняет аккаунт-наследник v1 значениями из ENV (см. scripts/migrate_v1.py)."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE accounts SET name=?, max_chat_title=?, owner_tg_id=?,"
                " max_self_name=?, profile_path=?, updated_at=? WHERE id=?",
                (name, chat_title, owner_tg_id, self_name, profile_path,
                 int(time.time()), account_id),
            )

    # ============================================================== accounts
    def create_account(
        self, name: str, *, phone: str = "", chat_title: str = "", chat_url: str = "",
        self_name: str = "", owner_tg_id: int | None = None, status: str = "stopped",
        profile_path: str = "",
    ) -> Account:
        now = int(time.time())
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO accounts (name, phone, max_chat_title, max_chat_url,"
                " max_self_name, owner_tg_id, status, profile_path, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, phone, chat_title, chat_url, self_name, owner_tg_id,
                 status, profile_path, now, now),
            )
            row = self._db.execute(
                "SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return Account.from_row(row)

    def update_account(self, account_id: int, **fields: Any) -> None:
        """Обновление полей accounts. Разрешённые ключи — колонки таблицы."""
        allowed = {"name", "phone", "max_chat_title", "max_chat_url", "max_self_name",
                   "owner_tg_id", "status", "profile_path"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"update_account: недопустимые поля {bad}")
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE accounts SET {cols} WHERE id = ?",
                (*fields.values(), account_id),
            )

    def delete_account(self, account_id: int) -> None:
        with self._lock, self._db:
            # listeners/message_map/seen чистим явно: ON DELETE CASCADE зависит от PRAGMA
            self._db.execute("DELETE FROM listeners WHERE account_id=?", (account_id,))
            self._db.execute("DELETE FROM message_map WHERE account_id=?", (account_id,))
            self._db.execute("DELETE FROM seen_message WHERE account_id=?", (account_id,))
            self._db.execute("DELETE FROM stats WHERE account_id=?", (account_id,))
            self._db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def get_account(self, account_id: int) -> Account | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        return Account.from_row(row) if row else None

    def get_account_by_name(self, name: str) -> Account | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
        return Account.from_row(row) if row else None

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [Account.from_row(r) for r in rows]

    def accounts_by_owner(self, tg_user_id: int) -> list[Account]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM accounts WHERE owner_tg_id = ? ORDER BY id", (tg_user_id,)
            ).fetchall()
        return [Account.from_row(r) for r in rows]

    # ============================================================ listeners
    def add_listener(self, account_id: int, tg_user_id: int, alias: str = "") -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO listeners (account_id, tg_user_id, alias, created_at)"
                " VALUES (?,?,?,?)",
                (account_id, tg_user_id, alias, int(time.time())),
            )
            return cur.rowcount > 0

    def remove_listener(self, account_id: int, tg_user_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM listeners WHERE account_id=? AND tg_user_id=?",
                (account_id, tg_user_id),
            )
            return cur.rowcount > 0

    def list_listeners(self, account_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM listeners WHERE account_id=? ORDER BY id", (account_id,)
            ).fetchall()

    def is_listener(self, account_id: int, tg_user_id: int) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM listeners WHERE account_id=? AND tg_user_id=?",
                (account_id, tg_user_id),
            ).fetchone() is not None

    def accounts_by_listener(self, tg_user_id: int) -> list[Account]:
        with self._lock:
            rows = self._db.execute(
                "SELECT a.* FROM accounts a JOIN listeners l ON l.account_id = a.id"
                " WHERE l.tg_user_id = ? ORDER BY a.id",
                (tg_user_id,),
            ).fetchall()
        return [Account.from_row(r) for r in rows]

    # ============================================================== fanout
    def recipients(self, account_id: int) -> tuple[int | None, list[int]]:
        """(owner_tg_id, [listener_tg_id, ...]) — адресаты входящих из MAX."""
        with self._lock:
            acc = self._db.execute(
                "SELECT owner_tg_id FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            rows = self._db.execute(
                "SELECT tg_user_id FROM listeners WHERE account_id=?", (account_id,)
            ).fetchall()
        owner = acc["owner_tg_id"] if acc and acc["owner_tg_id"] else None
        listeners = [r["tg_user_id"] for r in rows]
        return owner, listeners

    # =========================================================== message_map
    def remember_message(
        self, account_id: int, tg_user_id: int, tg_message_id: int,
        max_message_key: str, max_sender: str, max_preview: str,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO message_map"
                " (account_id, tg_user_id, tg_message_id, max_message_key,"
                "  max_sender, max_preview, created_at) VALUES (?,?,?,?,?,?,?)",
                (account_id, tg_user_id, tg_message_id, max_message_key,
                 max_sender, max_preview, int(time.time())),
            )

    def lookup_message(self, tg_message_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM message_map WHERE tg_message_id=?", (tg_message_id,)
            ).fetchone()

    # ================================================================== seen
    def mark_seen(self, account_id: int, key: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_message (account_id, key, first_seen)"
                " VALUES (?,?,?)",
                (account_id, key, int(time.time())),
            )
            return cur.rowcount > 0

    # ================================================================= stats
    def bump_stat(self, account_id: int, name: str, delta: int = 1) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO stats (account_id, name, value) VALUES (?,?,?)"
                " ON CONFLICT(account_id, name) DO UPDATE SET value = value + excluded.value",
                (account_id, name, delta),
            )

    def get_stats(self, account_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, value FROM stats WHERE account_id=?", (account_id,)
            ).fetchall()
        return {r["name"]: r["value"] for r in rows}

    # ================================================================== meta
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?,?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )

    # ================================================================ house
    def _purge_old(self) -> None:
        cutoff = int(time.time()) - self._retention_days * 86400
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen_message WHERE first_seen < ?", (cutoff,))
            self._db.execute("DELETE FROM message_map WHERE created_at < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
