"""
app/utils.py
============
Мелкие переиспользуемые утилиты: логирование, «человеческие» задержки,
retry с backoff, разбивка длинного текста под лимиты Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s"


def setup_logging(level: str | None = None) -> None:
    import os

    level = level or os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FMT,
        stream=sys.stdout,
    )
    # aiohttp очень разговорчив по умолчанию
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# «Человеческое» поведение (антифрод)
# ---------------------------------------------------------------------------

async def human_pause(lo: float = 1.0, hi: float = 3.0) -> float:
    """Случайная пауза перед осмысленным действием (клик/отправка)."""
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)
    return delay


async def jitter(lo: float = 0.05, hi: float = 0.18) -> None:
    """Микропауза между символами/шагами — как у живого человека."""
    await asyncio.sleep(random.uniform(lo, hi))


async def idle_chance(prob: float = 0.25, lo: float = 1.5, hi: float = 4.0) -> None:
    """Иногда «отвлекаемся» — убирать полную детерминированность таймингов."""
    if random.random() < prob:
        await asyncio.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

async def retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    what: str = "operation",
    logger: logging.Logger | None = None,
) -> T:
    """Универсальный retry с экспоненциальным backoff + джиттером."""
    log = logger or get_logger("retry")
    delay = base_delay
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except retry_on as exc:  # noqa: PERF203
            last_exc = exc
            if attempt == attempts:
                break
            sleep_for = min(max_delay, delay) * (1 + random.random() * 0.35)
            log.warning(
                "%s: попытка %d/%d упала (%s: %s), повтор через %.1f c",
                what, attempt, attempts, type(exc).__name__, exc, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay *= backoff
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

TG_TEXT_LIMIT = 4096


def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Режет длинный текст на куски <= limit по границам строк/пробелов."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return chunks


_WS_RE = re.compile(r"[ \t\u00a0]+")


def normalize_ws(text: str) -> str:
    """Схлопывает непереносимые пробелы/табы, сохраняет переносы строк."""
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def first_of(values: Iterable[str | None]) -> str:
    for value in values:
        if value:
            return value
    return ""


def truncate(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def now_ms() -> int:
    return int(time.time() * 1000)
