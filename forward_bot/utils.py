from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from forward_bot.crypto.obfuscation import temporal_id


def as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def resolve_forwarded_sender(repo: Any, viewer_id: int, replied_message_id: int) -> tuple[int, int] | None:
    return await repo.sender_by_delivery(viewer_id, replied_message_id)


async def resolve_whisper_sender(repo: Any, viewer_id: int, replied_message_id: int) -> int | None:
    return await repo.whisper_sender_by_reply(viewer_id, replied_message_id)


@dataclass(frozen=True)
class TargetResolution:
    user: Any | None
    consumed: int = 0
    error: str | None = None
    source_message_id: int | None = None


async def resolve_reply_target(repo: Any, viewer_id: int, replied_message_id: int) -> TargetResolution:
    lookup = await repo.sender_by_delivery(viewer_id, replied_message_id)
    if lookup is not None:
        message_id, sender_id = lookup
        return TargetResolution(await repo.get_user(sender_id), source_message_id=message_id)
    whisper = await repo.whisper_context_by_reply(viewer_id, replied_message_id)
    if whisper is not None:
        sender_id = int(whisper["sender_id"])
        return TargetResolution(await repo.get_user(sender_id), source_message_id=-int(whisper["id"]))
    return TargetResolution(None, error="Message is not in cache anymore")


async def resolve_user_reference(
    repo: Any,
    cfg: dict[str, Any],
    caller: Any,
    args: list[str],
) -> TargetResolution:
    if not args:
        return TargetResolution(None, error="Missing user reference.")

    token = args[0].strip()
    if not token:
        return TargetResolution(None, error="Missing user reference.")

    if token.startswith("@"):
        if not getattr(caller, "is_admin", False):
            return TargetResolution(None, error="Only admins can resolve @usernames.")
        user = await repo.get_user_by_username(token[1:])
        return TargetResolution(user, consumed=1, error=None if user else "User not found.")

    trip = _parse_tripcode_reference(args)
    if trip is not None:
        name, code, consumed = trip
        user = await _find_tripcode_user(repo, name, code)
        return TargetResolution(user, consumed=consumed, error=None if user else "User not found.")

    salt = str(cfg["bot"]["global_salt"])
    wanted = token.upper()
    for user in await repo.list_users():
        if temporal_id(user.telegram_id, salt) == wanted:
            return TargetResolution(user, consumed=1)
    return TargetResolution(None, error="User not found.")


def _parse_tripcode_reference(args: list[str]) -> tuple[str, str, int] | None:
    first = args[0].strip()
    if "!" in first:
        name, code = first.split("!", 1)
        name = name.strip()
        code = code.strip()
        if name and code:
            return name, code, 1
    if len(args) >= 2 and args[1].strip().startswith("!"):
        name = first
        code = args[1].strip()[1:]
        if name and code:
            return name, code, 2
    return None


async def _find_tripcode_user(repo: Any, name: str, code: str) -> Any | None:
    normalized_name = name.casefold()
    normalized_code = code[:6].casefold()
    for user in await repo.list_users():
        if not getattr(user, "tripcode_enabled", False):
            continue
        if not user.tripcode_name or not user.tripcode_hash:
            continue
        if str(user.tripcode_name).casefold() == normalized_name and str(user.tripcode_hash)[:6].casefold() == normalized_code:
            return user
    return None
