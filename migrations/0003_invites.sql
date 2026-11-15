-- ============================================================================
--  MAX ⇄ Telegram bridge v2.1 — инвайт-ссылки (deep linking)
--  Миграция 2 -> 3. Канон — app/store.py (_SCHEMA, _migrate_v2_to_v3,
--  PRAGMA user_version=3). Файл для чтения глазами и ручного SQL-диагноза:
--      docker compose exec bridge sqlite3 /data/state/bridge.db < migrations/0003_invites.sql
--  Идемпотентен (IF NOT EXISTS + проверка колонки через ALTER невозможно в чистом
--  SQL — повторный запуск даст «duplicate column», выполняйте один раз или
--  доверьтесь авто-миграции в store.py при старте приложения).
-- ============================================================================

PRAGMA user_version = 3;

-- Инвайты. expires_at/max_uses/used_count — числа (epoch-сек / счётчик),
-- как и везде в схеме: никакого DATETIME-парсинга в рантайме.
CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT    NOT NULL UNIQUE,   -- secrets.token_urlsafe(16)
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,          -- 'listener' | 'owner'
    max_uses    INTEGER NOT NULL DEFAULT 1,-- 0 = безлимит
    used_count  INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER,                   -- epoch-секунды или NULL = бессрочно
    created_at  INTEGER NOT NULL,
    CHECK (role IN ('listener','owner')),
    CHECK (max_uses >= 0 AND used_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_invites_account ON invites(account_id);
CREATE INDEX IF NOT EXISTS idx_invites_token   ON invites(token);

-- Человекочитаемые имена слушателей из инвайтов (совместимость с v2.0:
-- строки, добавленные вручную по ID, остаются с NULL в новых колонках).
ALTER TABLE listeners ADD COLUMN username  TEXT;
ALTER TABLE listeners ADD COLUMN full_name TEXT;

-- История:
--   v2 (user_version=2): accounts/listeners/message_map/seen_message/stats/meta.
--   v3: + invites, + listeners.username/full_name. Данные не переписываются.
--
-- Активация токена (redeem) выполняется атомарно в store.redeem_invite():
--   BEGIN IMMEDIATE;
--   UPDATE invites SET used_count = used_count + 1
--    WHERE token = ?
--      AND (expires_at IS NULL OR expires_at > :now)
--      AND (max_uses = 0 OR used_count < max_uses)
--      AND (role <> 'owner' OR used_count = 0);
--   rowcount == 0  -> 'expired'/'exhausted' (сверка по свежему SELECT);
--   rowcount == 1  -> применение роли (listeners UPSERT / accounts.owner_tg_id)
--                     в той же транзакции, затем COMMIT.
-- Два одновременных перехода по одноразовой ссылке: второй блокируется на
-- BEGIN IMMEDIATE, после разблокировки видит used_count=1 и получает
-- rowcount=0 => 'exhausted'. Гонка исключена и на уровне межпроцессной записи.
