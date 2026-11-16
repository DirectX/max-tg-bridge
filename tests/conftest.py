"""
tests/conftest.py
=================
Общие фикстуры для pytest-набора v2.2. Никакого браузера, никакого Telegram:
тесты гоняют чистую бизнес-логику (store/invites/worker-routing/backup) на
песочнице в tmp_path.

aiogram/playwright в модулях приложения импортируются на уровне модуля,
поэтому для импорта app.* они должны быть установлены (в контейнере — есть).
Если их нет (локальный запуск без deps) — тесты, где они не нужны, всё равно
работают: app.store/app.backup тянут только stdlib.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# корень проекта — чтобы `import app.*` работал без установки пакета
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Чистое окружение моста в tmp_path: все пути и ENV изолированы."""
    dirs = {
        "profiles": tmp_path / "profiles",
        "state": tmp_path / "state",
        "tmp": tmp_path / "tmp",
        "logs": tmp_path / "logs",
        "backups": tmp_path / "backups",
    }
    for d in dirs.values():
        d.mkdir(parents=True)
    env = {
        "TG_BOT_TOKEN": "123:test-token",
        "TG_API_BASE": "https://tg.test.example",
        "ADMIN_TG_ID": "900",
        "PROFILES_ROOT": str(dirs["profiles"]),
        "TRASH_ROOT": str(dirs["profiles"] / ".trash"),
        "TMP_DIR": str(dirs["tmp"]),
        "DB_PATH": str(dirs["state"] / "bridge.db"),
        "BROWSER_LOG": str(dirs["logs"] / "browser.log"),
        "SCREENSHOTS_DIR": str(dirs["logs"]),
        "BACKUPS_DIR": str(dirs["backups"]),
        "BACKUP_KEEP": "3",
        "BACKUP_PASSWORD": "",
        "MAX_CHAT_TITLE": "",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return {"root": tmp_path, "dirs": dirs, "env": env}


@pytest.fixture()
def cfg(sandbox):
    from app.config import load_config
    return load_config(require_admin=True)


@pytest.fixture()
def store(cfg):
    from app.store import Store
    st = Store(cfg.db_path)
    yield st
    st.close()


@pytest.fixture()
def tg_recorder(cfg):
    """Записывающий фейк Telegram (см. tests/fakes.py)."""
    from fakes import FakeTG
    return FakeTG(admin_id=cfg.admin_tg_id)
