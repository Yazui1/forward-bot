from __future__ import annotations

import asyncio
import logging
from collections import Counter
from threading import Lock
from typing import Any

from telegram.error import Forbidden, TelegramError


COMMON_TELEGRAM_ERROR_MARKERS = (
    "bot was blocked",
    "blocked by the user",
    "chat not found",
    "forbidden",
    "user is deactivated",
    "message to edit not found",
    "message is not modified",
    "message to be replied not found",
    "reply message not found",
    "message can't be deleted",
    "message can't be edited",
)

UNREACHABLE_TELEGRAM_ERROR_MARKERS = (
    "bot was blocked",
    "blocked by the user",
    "chat not found",
    "user is deactivated",
    "forbidden",
)


def configure_logging(level: str | int = "INFO") -> None:
    numeric = getattr(logging, str(level).upper(), logging.INFO) if isinstance(level, str) else level
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class AggregateLogger:
    def __init__(self, name: str = "forward_bot.aggregate") -> None:
        self.logger = logging.getLogger(name)
        self._lock = Lock()
        self._counters: Counter[str] = Counter()

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] += amount

    def flush(self) -> None:
        with self._lock:
            before = dict(self._counters)
            self._counters.clear()
            after = dict(self._counters)
        if before:
            self.logger.info("aggregate before=%s after=%s", before, after)


def log_telegram_error(
    logger: logging.Logger,
    action: str,
    exc: TelegramError,
    *,
    aggregate: AggregateLogger | None = None,
    repo: Any | None = None,
    user_id: int | None = None,
    **fields: Any,
) -> None:
    key = f"telegram.{action}.{exc.__class__.__name__}"
    if aggregate:
        aggregate.increment(key)
    target_user_id = user_id if user_id is not None else fields.get("user_id")
    if target_user_id is not None:
        fields = {**fields, "user_id": target_user_id}
    if repo is not None and target_user_id is not None and is_unreachable_telegram_error(exc):
        try:
            repo.mark_left(int(target_user_id))
            fields = {**fields, "marked_left": True}
            if aggregate:
                aggregate.increment("telegram.mark_left")
        except Exception:
            logger.exception("failed to mark user left after telegram error user_id=%s", target_user_id)
    text = str(exc).lower()
    routine = any(marker in text for marker in COMMON_TELEGRAM_ERROR_MARKERS)
    if routine:
        logger.info("telegram %s failed: %s fields=%s", action, exc, fields)
        return
    logger.warning("telegram %s failed: %s fields=%s", action, exc, fields, exc_info=True)


def is_unreachable_telegram_error(exc: TelegramError) -> bool:
    if isinstance(exc, Forbidden):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in UNREACHABLE_TELEGRAM_ERROR_MARKERS)


async def aggregate_log_worker(aggregate: AggregateLogger, interval_seconds: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(5, interval_seconds))
        except asyncio.TimeoutError:
            aggregate.flush()
    aggregate.flush()
