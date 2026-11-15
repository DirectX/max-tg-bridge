"""
app/store.py
============
Тонкая прослойка над стандартным sqlite3 (всегда есть в stdlib — ноль
зависимостей и ноль проблем с кросс-компиляцией на ARM64).

Хранит в ./data/state/bridge.db:

1. seen_message — id сообщений MAX, уже переданных в Telegram.
   Нужен, чтобы после рестарта контейнера мост не переслал историю заново.
2. msg_map       — соответствие tg_message_id -> max_* для режима «ответил
   в Telegram на пересланное сообщение → отправить как реплаей в MAX».

Все вызовы синхронные, но дешёвые (WAL, локальный том). Для объёмов
школьного чата этого более чем достаточно; sqlite-операции обёрнуты в
to_thread вызывающим кодом не требуются — микросекунды на запись.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .utils import get_logger

log = get_logger("store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_message (
    key        TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS msg_map (
    tg_message_id INTEGER PRIMARY KEY,
    max_node_key  TEXT,
    max_sender    TEXT,
    max_preview   TEXT,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self._path = path
        self._retention_days = retention_days
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row  # строки доступны и по индексу, и по имени
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
        self._purge_old()

    # ------------------------------------------------------------------ seen
    def is_seen(self, key: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen_message WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def mark_seen(self, key: str) -> bool:
        """True — ключ добавлен (новое сообщение); False — уже был."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_message (key, first_seen) VALUES (?, ?)",
                (key, int(time.time())),
            )
            return cur.rowcount > 0

    def _purge_old(self) -> None:
        cutoff = int(time.time()) - self._retention_days * 86400
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen_message WHERE first_seen < ?", (cutoff,))
            self._db.execute("DELETE FROM msg_map WHERE created_at < ?", (cutoff,))

    # ------------------------------------------------------------------- map
    def remember_tg_to_max(
        self,
        tg_message_id: int,
        max_node_key: str | None,
        max_sender: str,
        max_preview: str,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO msg_map "
                "(tg_message_id, max_node_key, max_sender, max_preview, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tg_message_id, max_node_key, max_sender, max_preview, int(time.time())),
            )

    def lookup_tg(self, tg_message_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM msg_map WHERE tg_message_id = ?", (tg_message_id,)
            ).fetchone()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT v FROM meta WHERE k = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
