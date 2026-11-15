-- ============================================================================
--  MAX ⇄ Telegram bridge v2 — справочная схема SQLite (актуальная версия)
--  Канонический источник — app/store.py (_SCHEMA_V2, PRAGMA user_version=2).
--  Файл предназначен для чтения глазами и ручного SQL-диагноза через
--  `docker compose exec bridge sqlite3 /data/state/bridge.db`.
-- ============================================================================

PRAGMA user_version = 2;

-- Аккаунты MAX. Один аккаунт = один изолированный браузер-профиль = один воркер.
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,            -- «Школа 5Б»
    phone           TEXT    DEFAULT '',                 -- справка для админа
    max_chat_title  TEXT    NOT NULL DEFAULT '',        -- чат MAX (поиск)
    max_chat_url    TEXT    NOT NULL DEFAULT '',        -- чат MAX (прямой URL)
    max_self_name   TEXT    NOT NULL DEFAULT '',        -- ник владельца в MAX (анти-эхо)
    owner_tg_id     INTEGER,                            -- ВЛАДЕЛЕЦ, 1-к-1, NULL=нет
    status          TEXT    NOT NULL DEFAULT 'stopped', -- active|stopped|auth_required|authenticating
    profile_path    TEXT    NOT NULL DEFAULT '',        -- /data/profiles/account_<id>
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- СЛУШАТЕЛИ: read-only получатели копий входящих из MAX.
-- Ответы в MAX им не доступны (проверка owner_tg_id в user_handlers).
CREATE TABLE IF NOT EXISTS listeners (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tg_user_id  INTEGER NOT NULL,
    alias       TEXT    DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE (account_id, tg_user_id)
);

-- КРИТИЧНАЯ ТАБЛИЦА фан-аута: одно сообщение MAX отправляется КАЖДОМУ
-- получателю отдельным tg-сообщением, поэтому ключ — пара (tg_user_id,
-- tg_message_id). Reply Владельца резолвится по tg_message_id -> узнаём
-- аккаунт, исходного отправителя и превью для цитаты в MAX.
CREATE TABLE IF NOT EXISTS message_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tg_user_id      INTEGER NOT NULL,
    tg_message_id   INTEGER NOT NULL UNIQUE,
    max_message_key TEXT    DEFAULT '',
    max_sender      TEXT    DEFAULT '',
    max_preview     TEXT    DEFAULT '',
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mm_account ON message_map(account_id);
CREATE INDEX IF NOT EXISTS idx_mm_user   ON message_map(account_id, tg_user_id);

-- Дедупликация входящих из MAX, посчётно на аккаунт (переживает рестарты).
CREATE TABLE IF NOT EXISTS seen_message (
    account_id INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (account_id, key)
);

-- Счётчики для админ-панели (переживают рестарты; live-значения — в воркере).
CREATE TABLE IF NOT EXISTS stats (
    account_id INTEGER NOT NULL,
    name       TEXT    NOT NULL,      -- max_to_tg | images | tg_to_max
    value      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, name)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- ---------------------------------------------------------------------------
-- История версий:
--   v1 (user_version=0 с таблицами): seen_message(key TEXT PK),
--      msg_map(tg_message_id...), meta  — общий чат, один аккаунт.
--   v2: миграция _migrate_v1_to_v2 в store.py: данные v1 переносятся в
--      «аккаунт-наследник», msg_map -> message_map (tg_user_id=0),
--      meta.k='migrated_from_v1' = timestamp. Физический перенос профиля
--      chrome_profile -> profiles/account_<id> делает scripts/migrate_v1.py.
