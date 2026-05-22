from __future__ import annotations

from datetime import date


def fnv32a(data: str) -> int:
    hash_value = 2166136261
    for b in data.encode("utf-8"):
        hash_value ^= b
        hash_value = (hash_value * 16777619) & 0xFFFFFFFF
    return hash_value


def temporal_id(telegram_id: int, global_salt: str, target_date: date | None = None) -> str:
    if target_date is None:
        target_date = date.today()
    source = f"{telegram_id}{global_salt}{target_date.isoformat()}"
    return f"{fnv32a(source):08X}"[:4]
