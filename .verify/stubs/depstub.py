"""Корректные офлайн-заглушки aiogram/playwright (только для .verify/)."""
import sys
import types


class PH:
    def __init__(self, *a, **k):
        pass
    def __getattr__(self, n):
        return PH()
    def __call__(self, *a, **k):
        return PH()
    def __and__(self, o):
        return PH()
    def __or__(self, o):
        return PH()
    def __invert__(self):
        return PH()
    def __eq__(self, o):
        return PH()
    def __ne__(self, o):
        return PH()
    def __hash__(self):
        return 1


class PwErr(Exception):
    pass


class State:
    """Реальный aiogram State — потомок MagicFilter: поддержаны & | ~ ==."""
    def __init__(self, *a, **k):
        pass
    def __and__(self, o):
        return PH()
    def __or__(self, o):
        return PH()
    def __invert__(self):
        return PH()
    def __eq__(self, o):
        return PH()
    def __hash__(self):
        return 2


class StatesGroup:
    pass


def _mk(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


_mk("playwright")
_mk("playwright.async_api", BrowserContext=PH, Error=PwErr, Page=PH, Playwright=PH,
    async_playwright=lambda: None, TimeoutError=PwErr, Locator=PH)
class _ParseMode:
    HTML = "HTML"
    MARKDOWN = "Markdown"
    MARKDOWNV2 = "MarkdownV2"

_mk("aiogram", Bot=PH, Dispatcher=PH, Router=PH, F=PH())
_mk("aiogram.enums", ParseMode=_ParseMode)
_mk("aiogram.client")
_mk("aiogram.client.telegram",
    TelegramAPIServer=type("T", (), {"from_base": staticmethod(lambda b: PH())}))
_mk("aiogram.client.default", DefaultBotProperties=PH)
_mk("aiogram.client.session")
_mk("aiogram.client.session.aiohttp",
    AiohttpSession=type("A", (), {"__init__": lambda s, **k: None}))
_mk("aiogram.exceptions", TelegramBadRequest=PwErr, TelegramRetryAfter=PwErr,
    TelegramForbiddenError=PwErr)
_mk("aiogram.filters", Command=PH, CommandStart=PH, StateFilter=type(
    "StateFilter", (), {"__init__": lambda s, *a, **k: None,
    "__invert__": lambda s: PH(), "__call__": lambda s, *a, **k: False}))
_mk("aiogram.fsm")
_mk("aiogram.fsm.context", FSMContext=object)
_mk("aiogram.fsm.state", State=State, StatesGroup=StatesGroup)
_mk("aiogram.fsm.storage")
_mk("aiogram.fsm.storage.memory", MemoryStorage=PH)
_mk("aiogram.types", BufferedInputFile=PH, FSInputFile=PH, Message=PH, CallbackQuery=PH,
    InlineKeyboardButton=PH, InlineKeyboardMarkup=PH)
_mk("aiogram.utils")
_mk("aiogram.utils.markdown", hbold=lambda x: x, hcode=lambda x: x)
