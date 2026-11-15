"""Офлайн-верификация v2 без установленных aiogram/playwright.

Запуск из корня проекта:  python3 .verify/run_checks.py
Покрывает: компиляцию, импорты, регистрацию роутеров, SQL-миграцию v1->v2,
CRUD ролей, фан-аут/дедуп/эхо в AccountWorker, жизненный цикл AccountManager,
JS-мост чтения (node), валидность docker compose config.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = ROOT / ".verify" / "stubs"
WORK = ROOT / ".verify" / "work"

os.environ.update({
    "TG_BOT_TOKEN": "123:stub", "TG_API_BASE": "https://tg.stub.example",
    "ADMIN_TG_ID": "9", "PROFILES_ROOT": str(WORK / "profiles"),
    "TRASH_ROOT": str(WORK / "profiles" / ".trash"), "TMP_DIR": str(WORK / "tmp"),
    "DB_PATH": str(WORK / "state" / "bridge.db"),
    "BROWSER_LOG": str(WORK / "logs" / "browser.log"),
    "SCREENSHOTS_DIR": str(WORK / "logs"),
})

sys.path.insert(0, str(STUBS))
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        def wrapped():
            # каждой проверке — своя БД/профили, чтобы не наследовать состояние
            tag = fn.__name__.replace("c_", "")
            wd = WORK / tag
            os.environ.update({
                "DB_PATH": str(wd / "state" / "bridge.db"),
                "PROFILES_ROOT": str(wd / "profiles"),
                "TRASH_ROOT": str(wd / "profiles" / ".trash"),
                "TMP_DIR": str(wd / "tmp"),
                "BROWSER_LOG": str(wd / "logs" / "browser.log"),
                "SCREENSHOTS_DIR": str(wd / "logs"),
            })
            try:
                fn()
                RESULTS.append((name, True, ""))
                print(f"  ✅ {name}")
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"  ❌ {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc(limit=4)
        wrapped.__name__ = fn.__name__
        return wrapped
    return deco


# ---------------------------------------------------------------- 1. imports
@check("компиляция всех модулей (compileall)")
def c_compile():
    r = subprocess.run([sys.executable, "-m", "compileall", "-q", "app", "scripts"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


@check("импорт всех 16 модулей против заглушек")
def c_imports():
    import depstub  # noqa: F401  регистрирует sys.modules
    import importlib
    mods = ["app.utils", "app.selectors", "app.store", "app.config", "app.browser",
            "app.max_dom", "app.bridge", "app.account_manager", "app.tg.bot",
            "app.tg.states", "app.tg.keyboards", "app.tg.auth_fsm",
            "app.tg.admin_handlers", "app.tg.user_handlers", "app.main", "app.auth"]
    for m in mods:
        importlib.import_module(m)


@check("регистрация роутеров auth → admin → user")
def c_routers():
    import depstub
    PH = depstub.PH
    from app.config import load_config
    from app.store import Store
    from app.tg import admin_handlers, auth_fsm, user_handlers
    cfg = load_config(require_admin=True)
    store = Store(cfg.db_path)
    ctrl = auth_fsm.register(PH(), cfg, store, PH(), PH())
    assert ctrl.start_add_account and ctrl.start_relogin
    admin_handlers.register(PH(), cfg, store, PH(), PH(), ctrl)
    user_handlers.register(PH(), cfg, store, PH(), PH())
    store.close()


# ----------------------------------------------------------------- 2. store
def _make_v1_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE seen_message (key TEXT PRIMARY KEY, first_seen INTEGER);
      CREATE TABLE msg_map (tg_message_id INTEGER PRIMARY KEY, max_node_key TEXT,
                            max_sender TEXT, max_preview TEXT, created_at INTEGER);
      CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
      INSERT INTO seen_message VALUES ('h1',1),('data-message-id=7',2);
      INSERT INTO msg_map VALUES (111,'h1','Мария','Не задали ДЗ',123),
                                 (222,NULL,'','[Фото из MAX]',124);
    """)
    c.commit()
    c.close()


@check("SQL-миграция v1→v2 (перенос seen/message_map, аккаунт-заглушка)")
def c_migration():
    from app.store import Store
    db = WORK / "state" / "v1.db"
    _make_v1_db(db)
    st = Store(db)
    assert st._version() == 2
    acc = st.list_accounts()[0]
    assert acc.name.startswith("(миграция v1") and acc.status == "stopped"
    assert st.mark_seen(acc.id, "h1") is False          # перенесённый seen живёт
    assert st.mark_seen(acc.id, "fresh") is True
    for mid, prev in ((111, "Не задали ДЗ"), (222, "[Фото из MAX]")):
        row = st.lookup_message(mid)
        assert row and row["account_id"] == acc.id and row["max_preview"] == prev, mid
    assert st.get_meta("migrated_from_v1")
    st.close()
    st2 = Store(db)                                      # идемпотентность
    assert st2._version() == 2 and len(st2.list_accounts()) == 1
    st2.close()


@check("CRUD ролей: accounts/listeners/message_map/stats/cascade")
def c_roles():
    from app.store import Store
    st = Store(WORK / "state" / "roles.db")
    a1 = st.create_account("Школа 5Б", chat_title="9 Б", owner_tg_id=100,
                           status="active", self_name="Иван П.")
    a2 = st.create_account("Робо", chat_title="Кружок", owner_tg_id=200)
    assert st.get_account_by_name("Школа 5Б").id == a1.id
    st.add_listener(a1.id, 101, "Мама"); st.add_listener(a1.id, 102, "Папа")
    assert not st.add_listener(a1.id, 101, "дубль")
    owner, ls = st.recipients(a1.id)
    assert owner == 100 and sorted(ls) == [101, 102]
    # один max_message_key → разные tg_message_id разным людям
    st.remember_message(a1.id, 100, 900, "k1", "Учитель", "Собрание")
    st.remember_message(a1.id, 101, 901, "k1", "Учитель", "Собрание")
    st.remember_message(a1.id, 102, 902, "k1", "Учитель", "Собрание")
    for uid, mid in ((100, 900), (101, 901), (102, 902)):
        row = st.lookup_message(mid)
        assert row["tg_user_id"] == uid and row["account_id"] == a1.id
    assert st.is_listener(a1.id, 101) and not st.is_listener(a1.id, 999)
    assert [a.id for a in st.accounts_by_owner(200)] == [a2.id]
    assert [a.id for a in st.accounts_by_listener(102)] == [a1.id]
    st.bump_stat(a1.id, "max_to_tg"); st.bump_stat(a1.id, "max_to_tg", 4)
    assert st.get_stats(a1.id)["max_to_tg"] == 5
    assert st.mark_seen(a2.id, "k1") is True             # дедуп изолирован по аккаунтам
    st.update_account(a1.id, status="auth_required")
    assert st.get_account(a1.id).status == "auth_required"
    try:
        st.update_account(a1.id, evil=1); raise AssertionError("принял левое поле")
    except ValueError:
        pass
    st.delete_account(a1.id)
    assert st.list_listeners(a1.id) == [] and st.lookup_message(900) is None
    assert len(st.list_accounts()) == 1
    st.close()


@check("e2e scripts/migrate_v1.py + перенос профиля v1 в account_<id>")
def c_migrate_script():
    db = WORK / "state" / "e2e.db"
    _make_v1_db(db)
    v1 = ROOT / "data" / "chrome_profile"
    shutil.rmtree(ROOT / "data", ignore_errors=True)
    v1.mkdir(parents=True)
    (v1 / "LOGIN_DATA").write_text("session")
    env = dict(os.environ, DB_PATH=str(db), MAX_CHAT_TITLE="9 Б",
               TG_CHAT_ID="55", MAX_SELF_NAME="Иван П.")
    r = subprocess.run([sys.executable, "scripts/migrate_v1.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    prof = Path(env["PROFILES_ROOT"]) / "account_1" / "LOGIN_DATA"
    assert prof.exists(), f"профиль не переехал: {prof}"
    r2 = subprocess.run([sys.executable, "scripts/migrate_v1.py"], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=120)
    assert r2.returncode == 0, r2.stderr[-600:]          # идемпотентно
    shutil.rmtree(ROOT / "data", ignore_errors=True)


# --------------------------------------------------------------- 3. worker
@check("AccountWorker: фан-аут владельцу+слушателям, дедуп, mine, эхо-петля")
def c_fanout():
    from app.bridge import AccountWorker
    from app.max_dom import MaxImage, MaxMessage
    from app.store import Store
    cfg = __import__("app.config", fromlist=["load_config"]).load_config()
    store = Store(cfg.db_path)
    acc = store.create_account("5Б", chat_title="9 Б", owner_tg_id=100,
                               self_name="Я Админ")
    store.add_listener(acc.id, 201, "Мама"); store.add_listener(acc.id, 202, "Папа")

    sent, mid = [], 500

    class FakeTG:
        async def send_text(self, chat_id, text, reply_to=None):
            nonlocal mid; mid += 1
            sent.append((chat_id, "text", text)); return mid
        async def send_photo(self, chat_id, data, caption="", filename="f", reply_to=None):
            nonlocal mid; mid += 1
            sent.append((chat_id, "photo", caption)); return mid
        async def send_typing(self, chat_id):
            pass

    w = AccountWorker(cfg, store, FakeTG(), acc)   # браузер не стартует: max=None

    async def run():
        msg = MaxMessage(key="data-message-id=k1", sender="Учитель",
                         text="Собрание в 18:00", ts=0)
        await w._forward_max_to_recipients(msg)
        assert [s[0] for s in sent] == [100, 201, 202], sent
        assert all(s[2].startswith("👤 Учитель") and "Собрание" in s[2] for s in sent)
        for uid, m in ((100, 501), (201, 502), (202, 503)):
            row = store.lookup_message(m)
            assert row and row["tg_user_id"] == uid and row["max_preview"] == "Собрание в 18:00"
        n = len(sent)
        await w._forward_max_to_recipients(msg)                     # дубль ключа
        assert len(sent) == n
        await w._forward_max_to_recipients(                          # mine
            MaxMessage(key="k2", sender="", text="мой ответ", mine=True))
        assert len(sent) == n
        await w._forward_max_to_recipients(                          # self name
            MaxMessage(key="k3", sender="Я Админ", text="привет всем"))
        assert len(sent) == n
        w._remember_outbound("Добрый день!")                         # эхо-окно
        await w._forward_max_to_recipients(
            MaxMessage(key="k4", sender="Учитель", text="Добрый день!"))
        assert len(sent) == n
        # счётчик — доставки: 1 сообщение × 3 получателя
        assert store.get_stats(acc.id).get("max_to_tg") == 3, store.get_stats(acc.id)
        # TG→MAX через нерабочий аккаунт корректно отклоняется менеджером
        from app.account_manager import AccountManager
        mgr = AccountManager(cfg, store, FakeTG())
        ok, note = mgr.submit_outgoing(acc.id, __import__("app.bridge",
                                             fromlist=["Outgoing"]).Outgoing(text="x"))
        assert not ok and "не запущен" in note, note
        await mgr.shutdown()

    asyncio.run(run())
    store.close()


@check("AccountManager: создание/старт/стоп/удаление + trash профиля")
def c_manager():
    from app.account_manager import AccountManager
    from app.bridge import Outgoing
    from app.config import load_config
    from app.store import Store
    cfg = load_config()
    store = Store(cfg.db_path)
    notices = []

    class FakeTG:
        async def notify_admin(self, text, reply_markup=None):
            notices.append(text); return 1
        async def send_text(self, chat_id, text, reply_to=None):
            notices.append(text); return 1

    async def run():
        mgr = AccountManager(cfg, store, FakeTG())
        acc = await mgr.create_account("Школа 5Б", "9 Б", owner_tg_id=100)
        assert acc.profile_path.endswith(f"account_{acc.id}")
        try:
            await mgr.create_account("Школа 5Б", "x"); raise AssertionError("дубль прошёл")
        except ValueError as e:
            assert "занято" in str(e)
        acc_empty = await mgr.create_account("Без чата", "")
        note = await mgr.start_account(acc_empty.id)
        assert "Не задан чат" in note
        note = await mgr.start_account(acc.id)
        assert "запускается" in note and acc.id in mgr._tasks
        await asyncio.sleep(0.3)                       # воркер упадёт на заглушке
        note = await mgr.stop_account(acc.id)
        assert "остановлен" in note and acc.id not in mgr._tasks
        assert store.get_account(acc.id).status == "stopped"
        snap = mgr.snapshot()
        assert {s["id"] for s in snap} == {acc.id, acc_empty.id}
        prof = cfg.account_profile_dir(acc.id); prof.mkdir(parents=True, exist_ok=True)
        (prof / "marker").write_text("x")
        note = await mgr.delete_account(acc.id)
        assert store.get_account(acc.id) is None and "удалён" in note
        assert any(cfg.trash_root.glob(f"account_{acc.id}_*")), "профиль не в trash"
        await mgr.shutdown()

    asyncio.run(run())
    store.close()


@check("normalize_phone / split_text / read_ram_mb (utils)")
def c_utils():
    from app.utils import normalize_phone, read_ram_mb, split_text
    assert normalize_phone("8 999 123-45-67") == "+79991234567"
    assert normalize_phone("+7 (999) 123-45-67") == "+79991234567"
    try:
        normalize_phone("123"); raise AssertionError("мусор прошёл")
    except ValueError:
        pass
    parts = split_text("а" * 3000 + "\n" + "б" * 3000, limit=4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 2
    used, limit = read_ram_mb()
    assert used is None or used > 0


# ------------------------------------------------------------------ 4. misc
@check("JS-мост чтения: tests/run_js_bridge_test.sh")
def c_js():
    node = shutil.which("node")
    if not node:
        RESULTS.append(("JS-мост чтения", True, "node не найден — пропущено"))
        print("  ⚠️  node не найден, пропущено")
        return
    r = subprocess.run(["bash", "tests/run_js_bridge_test.sh"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]


@check("docker compose config (валидность + platform arm64 + shm 4gb)")
def c_compose():
    docker = shutil.which("docker")
    env = dict(os.environ)
    if not docker:
        import yaml
        d = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        svc = d["services"]["bridge"]
        assert svc["platform"] == "linux/arm64"
        assert svc["shm_size"] == "4gb"
        assert svc["restart"] == "unless-stopped"
        print("  ⚠️  docker нет — проверили только YAML-поля")
        return
    env_file = ROOT / ".env"
    env_file.write_text((ROOT / ".env.example").read_text())
    try:
        r = subprocess.run([docker, "compose", "config", "--quiet"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stderr[-600:]
    finally:
        env_file.unlink(missing_ok=True)


@check("селекторы: все списки непустые, только str")
def c_selectors():
    from app import selectors as S
    bad = []
    for name in dir(S):
        if name.isupper():
            v = getattr(S, name)
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                bad.append(name)
    assert not bad, bad
    for key in ("PHONE_INPUT", "SMS_INPUT", "MESSAGE_NODE", "MESSAGE_TEXT", "CHAT_SCOPE",
                "COMPOSER", "FILE_INPUT", "MINE_MARKERS", "REPLY_CONTROL", "ATTACH_PREVIEW"):
        assert hasattr(S, key), key


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    print("Проверки v2 (офлайн):\n")
    for fn in (c_compile, c_imports, c_routers, c_migration, c_roles, c_migrate_script,
               c_fanout, c_manager, c_utils, c_js, c_compose, c_selectors):
        fn()
    ok = sum(1 for _, s, _ in RESULTS if s)
    failed = [(n, e) for n, s, e in RESULTS if not s]
    print(f"\n{'=' * 60}\nИТОГ: {ok}/{len(RESULTS)} пройдено")
    for n, e in failed:
        print(f"  ❌ {n}: {e}")
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
