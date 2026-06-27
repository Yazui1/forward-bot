from __future__ import annotations

import re

from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.utils import html_escape, temporal_id


def resolve_user_reference(repo: Repository, config: Config, reference: str, viewer: User | None = None) -> User | None:
    ref = reference.strip()
    if not ref:
        return None

    if ref.startswith("@"):
        return repo.find_by_username(ref) if viewer and viewer.is_admin else None

    trip = re.fullmatch(r"(.+?)\s*!([0-9a-fA-F]{6,12})", ref)
    if trip:
        user = repo.find_by_tripcode(trip.group(1).strip(), trip.group(2).lower())
        if user and (viewer and viewer.is_admin or user.tripcode_enabled):
            return user
        return None

    if re.fullmatch(r"\d+", ref) and viewer and viewer.is_admin:
        user = repo.get_user(int(ref))
        if user:
            return user

    salt = str(config.get("bot.global_salt", ""))
    for user in repo.list_users():
        if temporal_id(user.telegram_id, salt).lower() == ref.lower():
            return user
    return None


def display_identity(user: User | None, config: Config, *, viewer: User | None = None, admin_view: bool | None = None) -> str:
    if not user:
        return "unknown"
    is_admin_view = bool(viewer and viewer.is_admin) if admin_view is None else bool(admin_view)
    temp = temporal_id(user.telegram_id, str(config.get("bot.global_salt", "")))
    trip = _tripcode(user, include_disabled=is_admin_view)
    if is_admin_view:
        parts = []
        if trip:
            parts.append(trip)
        if user.username:
            parts.append(f"@{user.username}")
        if parts:
            return " ".join(parts)
        return temp
    return trip or temp


def display_identity_html(user: User | None, config: Config, *, viewer: User | None = None, admin_view: bool | None = None) -> str:
    if not user:
        return "unknown"
    is_admin_view = bool(viewer and viewer.is_admin) if admin_view is None else bool(admin_view)
    temp = temporal_id(user.telegram_id, str(config.get("bot.global_salt", "")))
    trip = _tripcode_html(user, include_disabled=is_admin_view)
    if is_admin_view:
        parts = []
        if trip:
            parts.append(trip)
        if user.username:
            parts.append(f"@{html_escape(user.username)}")
        if parts:
            return " ".join(parts)
        return html_escape(temp)
    return trip or html_escape(temp)


def _tripcode(user: User, *, include_disabled: bool) -> str | None:
    if not user.tripcode_name or not user.tripcode_hash:
        return None
    if not include_disabled and not user.tripcode_enabled:
        return None
    return f"{user.tripcode_name}!{user.tripcode_hash}"


def _tripcode_html(user: User, *, include_disabled: bool) -> str | None:
    if not user.tripcode_name or not user.tripcode_hash:
        return None
    if not include_disabled and not user.tripcode_enabled:
        return None
    return f"<b>{html_escape(user.tripcode_name)}</b> !{html_escape(user.tripcode_hash)}"
