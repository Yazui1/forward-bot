from __future__ import annotations

from forward_bot.utils import temporal_id


def display_user_id(telegram_id: int, salt: str) -> str:
    return temporal_id(telegram_id, salt)
