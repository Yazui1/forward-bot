from __future__ import annotations

import hashlib
import html
import math
import random
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).replace(microsecond=0).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def today_key() -> str:
    return now_utc().date().isoformat()


def round_credits(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def html_escape(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def normalize_emoji(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\ufe0f", "")


def random_token(size: int = 12) -> str:
    return secrets.token_urlsafe(size).replace("-", "").replace("_", "")[:size]


def temporal_id(telegram_id: int, salt: str, day: str | None = None) -> str:
    day = day or today_key()
    payload = f"{salt}:{day}:{telegram_id}".encode()
    digest = hashlib.blake2s(payload, digest_size=5).hexdigest()
    return digest.upper()


def parse_duration_seconds(text: str | None, default: int) -> tuple[int, str]:
    if not text:
        return default, ""
    parts = text.strip().split(maxsplit=1)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", parts[0], re.I)
    if not m:
        return default, text.strip()
    amount = float(m.group(1))
    unit = m.group(2).lower() or "s"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    reason = parts[1].strip() if len(parts) > 1 else ""
    return max(1, int(amount * mult)), reason


def seconds_left(until: datetime | str | None) -> int:
    if isinstance(until, str):
        until = parse_dt(until)
    if not until:
        return 0
    return max(0, int((until - now_utc()).total_seconds()))


def human_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{math.ceil(seconds / 60)}m"
    if seconds < 86400:
        return f"{math.ceil(seconds / 3600)}h"
    return f"{math.ceil(seconds / 86400)}d"


def mean_median(values: list[float]) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        med = ordered[mid]
    else:
        med = (ordered[mid - 1] + ordered[mid]) / 2
    return ordered[0], med, ordered[-1]


def pick_random(items: Iterable[str]) -> str | None:
    seq = list(items)
    return random.choice(seq) if seq else None
