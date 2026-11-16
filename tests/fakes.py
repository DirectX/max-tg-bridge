"""
tests/fakes.py
==============
Записывающие фейки вместо Telegram/браузера для мок-тестов маршрутизации.
Никакой сети, никаких aiogram-объектов: только простые классы, чьи методы
совпадают по сигнатурам с реальными (TelegramCore / AccountManager / FSMContext).
"""

from __future__ import annotations

from types import SimpleNamespace


class FakeMessage:
    """Минимальный заменитель aiogram.types.Message для прямых вызовов хендлеров."""

    def __init__(self, *, message_id: int = 1, uid: int = 100, username=None,
                 full_name: str = "Тест", text=None, caption=None, photo=None,
                 chat_type: str = "private", chat_id=None,
                 reply_to_message=None, is_bot: bool = False):
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=uid, username=username,
                                         full_name=full_name, is_bot=is_bot,
                                         last_name=None)
        self.chat = SimpleNamespace(id=chat_id if chat_id is not None else uid,
                                    type=chat_type)
        self.text = text
        self.caption = caption
        self.photo = photo
        self.reply_to_message = reply_to_message
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        # «отправленные» сообщения: реальный aiogram answer() возвращает Message,
        # и хендлеры потом делают note.edit_text(...) — правки прилетают сюда
        self.sent_messages: list["FakeMessage"] = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        # реальный aiogram возвращает Message — у него есть edit_text/answer;
        # хендлеры (/_run_backup_) дергают edit_text по «отправленному» сообщению
        sent = FakeMessage(message_id=self.message_id + 5000, uid=self.from_user.id)
        sent.text = text
        self.sent_messages.append(sent)
        return sent

    async def reply(self, text, **kwargs):
        return await self.answer(text, **kwargs)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self

    @property
    def last_answer(self) -> str:
        return self.answers[-1][0] if self.answers else ""

    @property
    def all_answers_text(self) -> str:
        return "\n".join(t for t, _ in self.answers)

    @property
    def all_edits_text(self) -> str:
        """Тексты правок и своего сообщения, и всех «отправленных»."""
        chunks = [t for t, _ in self.edits]
        for m in self.sent_messages:
            chunks.extend(t for t, _ in m.edits)
        return "\n".join(chunks)


class FakeCallback:
    def __init__(self, data: str, uid: int = 900, username=None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, username=username,
                                         full_name="Админ", is_bot=False)
        self.message = FakeMessage(uid=uid)
        self.answers: list[str] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text or "")


class FakeState:
    """Заменитель aiogram.fsm.context.FSMContext (только используемые методы)."""

    def __init__(self):
        self.data: dict = {}
        self.state = None
        self.cleared = 0

    async def clear(self):
        self.data.clear()
        self.state = None
        self.cleared += 1

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return self.state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent) + 7000)


class FakeTG:
    """Записывающий «Telegram»: тексты, фото, файлы, throttled-ack, админ-пуши."""

    def __init__(self, admin_id: int = 900):
        self.admin_id = admin_id
        self.bot = FakeBot()
        self.texts: list[tuple[int, str]] = []
        self.photos: list[tuple[int, int, str, str]] = []
        self.files: list[tuple[int, int, str, str]] = []
        self.acks: list[tuple[int, str]] = []
        self.admin_notes: list[tuple[str, object]] = []
        self._mid = 1000

    def _next(self) -> int:
        self._mid += 1
        return self._mid

    async def send_text(self, chat_id, text, reply_to=None):
        self.texts.append((chat_id, text))
        return self._next()

    async def send_photo(self, chat_id, data, caption="", filename="f.png",
                         reply_to=None):
        self.photos.append((chat_id, len(data), caption, filename))
        return self._next()

    async def send_file_bytes(self, chat_id, data, filename, caption=""):
        self.files.append((chat_id, len(data), filename, caption))
        return self._next()

    async def notify_admin(self, text, reply_markup=None):
        self.admin_notes.append((text, reply_markup))
        return self._next()

    async def throttled_ack(self, chat_id, note, window_s=300.0, key=None):
        self.acks.append((chat_id, note))

    async def send_typing(self, chat_id) -> None:
        pass

    async def extract_photo(self, message):
        return None

    def invite_link(self, token):
        return f"https://t.me/my_bridge?start=inv_{token}"

    def chats_of(self, kind: str) -> list[int]:
        src = {"text": self.texts, "photos": self.photos, "files": self.files}[kind]
        return [row[0] for row in src]


class FakeMgr:
    """Перехватывает submit_outgoing вместо AccountManager."""

    def __init__(self, ok: bool = True, note: str = "Отправляю в MAX…"):
        self.calls: list[tuple[int, object]] = []
        self.ok = ok
        self.note = note

    def submit_outgoing(self, account_id: int, out):
        self.calls.append((account_id, out))
        return self.ok, self.note


# ---------------------------------------------------------------------------
# Извлечение реальных хендлеров из Router + сборка роутеров
# ---------------------------------------------------------------------------
def handlers_of(router, event: str = "message"):
    return {h.callback.__name__: h for h in getattr(router, event).handlers}


def call_callback(router, name: str, event: str = "message", **kwargs):
    """Дёргает НАСТОЯЩИЙ хендлер по имени с нужными аргументами."""
    return handlers_of(router, event)[name].callback(**kwargs)


def build_user_router(cfg, store, tg, mgr):
    from aiogram import Router
    from app.tg import user_handlers
    r = Router()
    user_handlers.register(r, cfg, store, tg, mgr)
    return r


def build_admin_router(cfg, store, tg, mgr, auth=None):
    from types import SimpleNamespace
    from aiogram import Router
    from app.tg import admin_handlers
    r = Router()
    admin_handlers.register(r, cfg, store, tg, mgr, auth or SimpleNamespace(
        start_add_account=lambda *a, **k: None,
        start_relogin=lambda *a, **k: None))
    return r
