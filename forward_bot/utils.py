from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def resolve_forwarded_sender(repo: Any, viewer_id: int, replied_message_id: int) -> tuple[int, int] | None:
    return await repo.sender_by_delivery(viewer_id, replied_message_id)


async def resolve_whisper_sender(repo: Any, viewer_id: int, replied_message_id: int) -> int | None:
    return await repo.whisper_sender_by_reply(viewer_id, replied_message_id)
