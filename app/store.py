"""
app/store.py  (v2.1)
====================
Хранилище ролевой модели и состояния моста. SQLite, стандартный sqlite3.
Схема версионируется через PRAGMA user_version; миграции — функции вида
``_migrate_vN_to_vM``. Текущая версия: 3.

Версии:
  0 -> 1 : первичная v1-схема (seen_message/msg_map/meta) — историческая
  1 -> 2 : v2 Multi-Account & Role-Based:
           accounts, listeners, message_map; seen_message с account_id.
  2 -> 3 : v2.1 Инвайт-ссылки (deep linking):
           новая таблица invites; у listeners добавлены username/full_name
           (совместимо с ручным добавлением по ID из v2.0 — колонки NULL).

Все записи — короткие транзакции в WAL; вызовы синхронные (микросекунды,
отдельный пул потоков не нужен при 2-5 аккаунтах и школьных объёмах).
"""

from __future__ import annotations

import contextlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger("store")

SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# Схема v3 (канон: CREATE TABLE IF NOT EXISTS — безопасна поверх v2)
# ---------------------------------------------------------------------------
_SCHEMA = """
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
    username    TEXT,                                  -- v3: @username (nullable)
    full_name   TEXT,                                  -- v3: имя (nullable)
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

-- v2.1: инвайт-ссылки (deep linking) для саморегистрации.
-- expires_at/max_uses/used_count — числа (epoch-секунды / счётчик),
-- как и везде в этой схеме: никакого DATETIME-парсинга в рантайме.
CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT    NOT NULL UNIQUE,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,                      -- 'listener' | 'owner'
    max_uses    INTEGER NOT NULL DEFAULT 1,            -- 0 = безлимит
    used_count  INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER,                               -- epoch или NULL = бессрочно
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invites_account ON invites(account_id);
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
                    log.info("Найдена v1-база — мигрируем в схему v%d", SCHEMA_VERSION)
                    self._migrate_v1_to_v2()
                else:
                    self._db.executescript(_SCHEMA)
                    log.info("Создана свежая v%d-база", SCHEMA_VERSION)
                self._set_version(SCHEMA_VERSION)
            elif 0 < ver < SCHEMA_VERSION:
                # пошагово: v2 -> v3 (и т.д. в будущем)
                if ver < 3:
                    self._migrate_v2_to_v3()
                self._set_version(SCHEMA_VERSION)
            # проверка целостности минимальная
            missing = [t for t in ("accounts", "listeners", "message_map",
                                   "seen_message", "invites")
                       if not self._table_exists(t)]
            if missing:
                raise RuntimeError(f"БД повреждена: отсутствуют таблицы {missing}")

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._db.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)

    def _migrate_v2_to_v3(self) -> None:
        """v2.1: таблица invites + колонки listeners.username/full_name.
        Данные v2.0 не трогаем: новые колонки nullable, старые строки = NULL."""
        log.info("Миграция v2 -> v3: invites, listeners.username/full_name")
        self._db.executescript(_SCHEMA)  # CREATE ... IF NOT EXISTS (включая invites)
        for col in ("username", "full_name"):
            if not self._column_exists("listeners", col):
                self._db.execute(f"ALTER TABLE listeners ADD COLUMN {col} TEXT")
                log.info("listeners: добавлена колонка %s", col)
        self._db.commit()

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
        self._db.executescript(_SCHEMA)

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
            # listeners/invites/message_map/seen чистим явно: ON DELETE CASCADE
            # зависит от PRAGMA foreign_keys, которое не всякий клиент ставит
            self._db.execute("DELETE FROM listeners WHERE account_id=?", (account_id,))
            self._db.execute("DELETE FROM invites WHERE account_id=?", (account_id,))
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
    def _apply_listener(self, account_id: int, tg_user_id: int, alias: str,
                        username: str | None, full_name: str | None) -> bool:
        """UPSERT слушателя БЕЗ управления транзакцией (вызывающий держит её).
        True — слушатель добавлен новым."""
        was = self._db.execute(
            "SELECT 1 FROM listeners WHERE account_id=? AND tg_user_id=?",
            (account_id, tg_user_id),
        ).fetchone() is not None
        self._db.execute(
            "INSERT INTO listeners (account_id, tg_user_id, alias, username,"
            " full_name, created_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(account_id, tg_user_id) DO UPDATE SET"
            "   alias = COALESCE(NULLIF(excluded.alias,''), listeners.alias),"
            "   username = COALESCE(excluded.username, listeners.username),"
            "   full_name = COALESCE(excluded.full_name, listeners.full_name)",
            (account_id, tg_user_id, alias, username, full_name, int(time.time())),
        )
        return not was

    def add_listener(self, account_id: int, tg_user_id: int, alias: str = "",
                     *, username: str | None = None, full_name: str | None = None) -> bool:
        """Добавить слушателя (или обновить его имя/@username, если он уже есть).

        Возвращает True, если слушатель ДОБАВЛЕН новым, False — если уже был.
        Совместимо с v2.0: username/full_name опциональны (ручной ввод по ID).
        """
        with self._lock, self._db:
            return self._apply_listener(account_id, tg_user_id, alias, username, full_name)

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

    # ================================================================ invites
    @contextlib.contextmanager
    def _immediate(self):
        """Явная BEGIN IMMEDIATE-транзакция (блокирует запись на весь блок).
        Безопасно сосуществует с ленивыми транзакциями sqlite3: если она уже
        открыта — переиспользуем, не начинаем вложенную."""
        with self._lock:
            started = not self._db.in_transaction
            if started:
                # явный BEGIN IMMEDIATE: блокирует запись во всей БД на время
                # блока — второй процесс/таска не проскочит с тем же токеном
                self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                if started:
                    self._db.commit()
            except BaseException:
                if started:
                    self._db.rollback()
                raise

    def create_invite(self, account_id: int, role: str, *, max_uses: int = 1,
                      ttl_seconds: int | None = None,
                      token: str | None = None) -> sqlite3.Row:
        """Создать инвайт. role: 'listener' | 'owner'.
        max_uses=0 — безлимит; owner всегда строго одноразовый (force)."""
        if role not in ("listener", "owner"):
            raise ValueError(f"Недопустимая роль инвайта: {role!r}")
        if role == "owner":
            max_uses = 1  # привязка владельца — только один раз
        token = token or secrets.token_urlsafe(16)
        now = int(time.time())
        expires_at = (now + ttl_seconds) if ttl_seconds else None
        with self._immediate():
            # на всякий случай переберём токен при (маловероятной) коллизии;
            # нарушение FK (аккаунт удалён) — НЕ коллизия, пробрасываем честно
            for attempt in range(5):
                try:
                    self._db.execute(
                        "INSERT INTO invites (token, account_id, role, max_uses,"
                        " used_count, expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                        (token, account_id, role, max_uses, 0, expires_at, now),
                    )
                    break
                except sqlite3.IntegrityError as exc:
                    msg = str(exc).lower()
                    if "unique" in msg and attempt < 4:
                        token = secrets.token_urlsafe(16)
                        continue
                    if "foreign key" in msg:
                        raise ValueError(
                            f"Аккаунт #{account_id} не существует — инвайт не создан"
                        ) from exc
                    raise
            else:  # pragma: no cover
                raise RuntimeError("Не удалось сгенерировать уникальный токен инвайта")
            row = self._db.execute(
                "SELECT * FROM invites WHERE token=?", (token,)
            ).fetchone()
        return row

    def list_invites(self, account_id: int, *, active_only: bool = True) -> list[sqlite3.Row]:
        """Список инвайтов аккаунта. active_only — скрыть исчерпанные/просроченные."""
        now = int(time.time())
        sql = "SELECT * FROM invites WHERE account_id=?"
        if active_only:
            sql += (" AND (expires_at IS NULL OR expires_at > ?)"
                    " AND (max_uses = 0 OR used_count < max_uses)")
        sql += " ORDER BY id DESC"
        with self._lock:
            # порядок параметров = порядок «?» в запросе: account_id, затем now
            params = (account_id, now) if active_only else (account_id,)
            return self._db.execute(sql, params).fetchall()

    def get_invite(self, token: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM invites WHERE token=?", (token,)
            ).fetchone()

    def revoke_invite(self, invite_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM invites WHERE id=?", (invite_id,))
            return cur.rowcount > 0

    def redeem_invite(self, token: str, tg_user_id: int, full_name: str,
                      username: str | None) -> tuple[str, sqlite3.Row | None, Account | None]:
        """АТОМАРНО активировать инвайт и применить его.

        Возвращает (status, invite_row, account):
          'ok_listener' | 'ok_owner'  — успех (счётчик уже увеличен);
          'invalid'  — токена нет;
          'expired'  — истёк срок;
          'exhausted'— исчерпан лимит (для owner — уже использован);
          'no_account' — аккаунт инвайта удалён.

        Гонка двух людей по одноразовой ссылке исключена: increment — это
        guarded UPDATE ... WHERE (условия лимита в самой строке), выполняемый
        внутри BEGIN IMMEDIATE. Второй пришедший увидит rowcount=0 и получит
        'exhausted', даже если оба запроса стартовали одновременно.
        """
        now = int(time.time())
        with self._immediate():
            row = self._db.execute(
                "SELECT * FROM invites WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                return "invalid", None, None
            # Guarded increment: условия лимита — прямо в WHERE.
            cur = self._db.execute(
                "UPDATE invites SET used_count = used_count + 1"
                " WHERE token = ?"
                "   AND (expires_at IS NULL OR expires_at > ?)"
                "   AND (max_uses = 0 OR used_count < max_uses)"
                "   AND (role <> 'owner' OR used_count = 0)",
                (token, now),
            )
            if cur.rowcount == 0:
                fresh = self._db.execute(
                    "SELECT * FROM invites WHERE token=?", (token,)
                ).fetchone()
                if fresh and fresh["expires_at"] is not None and fresh["expires_at"] <= now:
                    return "expired", fresh, None
                return "exhausted", fresh, None
            invite = self._db.execute(
                "SELECT * FROM invites WHERE token=?", (token,)
            ).fetchone()
            acc_row = self._db.execute(
                "SELECT * FROM accounts WHERE id=?", (invite["account_id"],)
            ).fetchone()
            if acc_row is None:
                # аккаунт удалён, а счётчик уже потрачен — откатываем increment
                self._db.execute(
                    "UPDATE invites SET used_count = used_count - 1 WHERE token=?",
                    (token,),
                )
                return "no_account", invite, None
            account = Account.from_row(acc_row)
            if invite["role"] == "owner":
                self._db.execute(
                    "UPDATE accounts SET owner_tg_id=?, updated_at=? WHERE id=?",
                    (tg_user_id, now, account.id),
                )
                status = "ok_owner"
            else:
                # _apply_listener (не add_listener): транзакцию держим нашу
                self._apply_listener(account.id, tg_user_id, "", username, full_name)
                status = "ok_listener"
        return status, invite, account

    async def redeem_invite_async(self, token: str, tg_user_id: int,
                                   full_name: str, username: str | None):
        """Async-обёртка redeem_invite для вызова из event loop моста/тестов.

        sqlite-блокировка захватывает только запись в БД (микросекунды), но
        держать её в loop-потоке нельзя: уводим в to_thread. Гонка двух
        корутин по одноразовому токену разрешается корректно: обе попадают
        в разные потоки, вторая видит уже used_count=1 (BEGIN IMMEDIATE).
        """
        import asyncio
        return await asyncio.to_thread(self.redeem_invite, token, tg_user_id,
                                       full_name, username)

    # ================================================================ house
    def _purge_old(self) -> None:
        cutoff = int(time.time()) - self._retention_days * 86400
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen_message WHERE first_seen < ?", (cutoff,))
            self._db.execute("DELETE FROM message_map WHERE created_at < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
