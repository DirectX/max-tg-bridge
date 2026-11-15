#!/usr/bin/env python3
"""
scripts/migrate_v1.py
=====================
Пошаговая миграция прототипа v1 (один общий мост) в v2 (Multi-Account).

Что делает (идемпотентно):
  1. SQL: store.py при первом открытии БД сам распознаёт v1-схему и
     поднимает v2, перенося seen_message и msg_map в «аккаунт-наследник»
     (этот шаг запускается здесь косвенно — открытием Store).
  2. ENV-сид: заполняет аккаунт-наследник значениями из .env v1
     (MAX_CHAT_TITLE / MAX_CHAT_URL / MAX_SELF_NAME / TG_CHAT_ID -> владелец).
  3. Файлы: переносит ./data/chrome_profile (профиль v1) в
     ./data/profiles/account_<id>/ — сессия MAX сохраняется, заново
     вводить номер и код НЕ нужно (в крайнем случае скажет бот).
  4. Печатает чек-лист дальнейших действий.

Запуск из корня проекта (в контейнере или локально с тем же .env):
    docker compose run --rm bridge python scripts/migrate_v1.py --dry-run
    docker compose run --rm bridge python scripts/migrate_v1.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Разрешаем запуск и из /app (контейнер), и из корня репозитория
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config          # noqa: E402
from app.store import Store                  # noqa: E402
from app.utils import setup_logging          # noqa: E402

V1_ACCOUNT_NAME = "Аккаунт 1 (из v1)"
PLACEHOLDER = "(миграция v1: заполните в /admin)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Миграция БД/профилей v1 -> v2")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что будет сделано")
    parser.add_argument("--name", default=V1_ACCOUNT_NAME,
                        help="имя аккаунта-наследника")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(require_admin=True)
    v1_profile = Path("/data/chrome_profile")
    if not v1_profile.exists():
        v1_profile = Path("./data/chrome_profile")

    # Шаг 1: открытие Store выполняет SQL-миграцию (создаст аккаунт-заглушку)
    store = Store(cfg.db_path)
    migrated_at = store.get_meta("migrated_from_v1")
    print(f"SQL-миграция: {'выполнена (' + migrated_at + ')' if migrated_at else 'не требовалась (база уже v2)'}")

    # Шаг 2: найти аккаунт-заглушку и засеять из ENV
    target = None
    for acc in store.list_accounts():
        if acc.name == PLACEHOLDER or (not acc.max_chat_title and not acc.max_chat_url):
            target = acc
            break

    if target is not None:
        if not (cfg.legacy_max_chat_title or cfg.legacy_max_chat_url):
            print("⚠️  В .env нет MAX_CHAT_TITLE/MAX_CHAT_URL — аккаунт-наследник "
                  "создан без чата. Укажите в /admin или допишите ENV и перезапустите.")
        seed = {
            "name": args.name,
            "chat_title": cfg.legacy_max_chat_title or args.name,
            "owner_tg_id": cfg.legacy_tg_chat_id or None,
            "profile_path": str(cfg.account_profile_dir(target.id)),
            "self_name": cfg.legacy_max_self_name,
        }
        print(f"Засеваем аккаунт #{target.id}: name={seed['name']!r} "
              f"chat={seed['chat_title']!r} owner={seed['owner_tg_id']} "
              f"profile={seed['profile_path']}")
        if not args.dry_run:
            store.seed_migrated_account(target.id, **seed)
    else:
        print("Аккаунт-заглушка не найден — посев уже был или база свежая.")

    # Шаг 3: перенос профиля v1 в каноническую папку
    acc = target or (store.list_accounts() or [None])[0]
    if acc is not None:
        dest = cfg.account_profile_dir(acc.id)
        src = v1_profile if v1_profile.is_dir() else None
        if src is not None and src.exists() and str(src) != str(dest):
            print(f"Профиль: {src} -> {dest}")
            if not args.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    print("   ⚠️  Каталог назначения уже существует — НЕ ТРОГАЮ. "
                          "Сравните вручную и удалите лишний.")
                else:
                    shutil.move(str(src), str(dest))
                    # src уже не существует: shutil.move перенёс каталог целиком
        elif src is None:
            print(f"Профиль v1 ({v1_profile}) не найден — пропуск "
                  "(аккаунт попросит авторизацию через бота).")

    # Шаг 4: чек-лист
    print("""
───────────────────────────────────────────────────────────────
 Дальнейшие шаги:
   1. Убедитесь, что в .env задан ADMIN_TG_ID.
   2. docker compose up -d --build
   3. В Telegram: /admin — аккаунт должен быть в списке.
      • Если статус 🔴 — нажмите «Переавторизовать» и введите код из СМС.
      • Назначьте слушателей: /admin → аккаунт → «Слушатели».
   4. Строки TG_CHAT_ID / MAX_CHAT_* из старого .env можно удалить.
───────────────────────────────────────────────────────────────
""")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
