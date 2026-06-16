from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from forward_bot.cache.transient import TransientStore
from forward_bot.features.credits import round_credit
from forward_bot.utils import as_utc

logger = logging.getLogger(__name__)


class ManagedConnection:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def __aenter__(self) -> aiosqlite.Connection:
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


@dataclass
class User:
    telegram_id: int
    username: str | None
    has_started: bool
    is_banned: bool
    is_moderator: bool
    is_admin: bool
    created_at: str | None
    last_activity: str | None
    confirmation_enabled: bool
    votes_enabled: bool
    vote_buttons_enabled: bool
    hide_potentially_unwanted: bool
    filter_duplicates: bool
    fights_enabled: bool
    sign_enabled: bool
    tripcode_enabled: bool
    tripcode_name: str | None
    tripcode_hash: str | None
    about_seen: bool
    credits: float


class Repository:
    def __init__(self, db_path: str, transient_ttl_seconds: int = 259200) -> None:
        self.db_path = db_path
        self.transient = TransientStore(ttl_seconds=transient_ttl_seconds)
        self._user_cache: dict[int, User] = {}
        self._all_users_loaded = False
        self._blocks_cache_loaded = False
        self._blocks_by_blocker: dict[int, set[int]] = {}
        self._media_hash_cache_loaded = False
        self._media_hash_first_seen: dict[str, str] = {}
        self._media_hash_latest_seen: dict[str, str] = {}
        self._blocked_sticker_sets_loaded = False
        self._blocked_sticker_sets: set[str] = set()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _cache_user(self, user: User) -> User:
        self._user_cache[user.telegram_id] = user
        return user

    def _cache_users(self, users: list[User]) -> list[User]:
        for user in users:
            self._cache_user(user)
        self._all_users_loaded = True
        return users

    async def _refresh_user_cache(self, telegram_id: int) -> User | None:
        conn = await self._conn()
        async with conn:
            row = await (await conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))).fetchone()
        if row is None:
            self._user_cache.pop(telegram_id, None)
            return None
        return self._cache_user(self._to_user(row))

    def _invalidate_all_users_cache(self) -> None:
        self._all_users_loaded = False

    async def _conn(self) -> ManagedConnection:
        conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout = 30000")
        await conn.execute("PRAGMA synchronous = NORMAL")
        return ManagedConnection(conn)

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None,
        admin_ids: set[int],
        starting_credits: float = 20.0,
    ) -> User:
        starting_credits = round_credit(starting_credits)
        conn = await self._conn()
        async with conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO users (telegram_id, username, is_admin, credits)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, username,
                 1 if telegram_id in admin_ids else 0, starting_credits),
            )
            await conn.execute(
                "UPDATE users SET credits = ? WHERE telegram_id = ? AND credits IS NULL",
                (starting_credits, telegram_id),
            )
            await conn.execute(
                "UPDATE users SET username = COALESCE(?, username) WHERE telegram_id = ?",
                (username, telegram_id),
            )
            await conn.execute(
                "UPDATE users SET is_admin = ? WHERE telegram_id = ?",
                (1 if telegram_id in admin_ids else 0, telegram_id),
            )
            await conn.commit()
            row = await (await conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))).fetchone()
        return self._cache_user(self._to_user(row))

    async def sync_admin_ids(self, admin_ids: set[int]) -> None:
        conn = await self._conn()
        async with conn:
            if not admin_ids:
                await conn.execute("UPDATE users SET is_admin = 0")
                await conn.commit()
                return
            placeholders = ",".join("?" for _ in admin_ids)
            await conn.execute(
                f"UPDATE users SET is_admin = CASE WHEN telegram_id IN ({placeholders}) THEN 1 ELSE 0 END",
                tuple(admin_ids),
            )
            await conn.commit()
        self._user_cache.clear()
        self._invalidate_all_users_cache()

    async def update_started(self, telegram_id: int, started: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET has_started = ?, last_activity = CURRENT_TIMESTAMP WHERE telegram_id = ?",
                (1 if started else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def mark_left(self, telegram_id: int) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET has_started = 0 WHERE telegram_id = ?",
                (telegram_id,),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def touch_activity(self, telegram_id: int) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE telegram_id = ?", (telegram_id,))
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def get_user_by_username(self, username: str) -> User | None:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1",
                    (username,),
                )
            ).fetchone()
        return None if row is None else self._cache_user(self._to_user(row))

    async def list_users(self) -> list[User]:
        if self._all_users_loaded:
            return list(self._user_cache.values())
        conn = await self._conn()
        async with conn:
            rows = await (await conn.execute("SELECT * FROM users")).fetchall()
        users = [self._to_user(row) for row in rows]
        self._user_cache = {}
        return self._cache_users(users)

    async def user_counts(self, inactive_days: int) -> dict[str, int]:
        conn = await self._conn()
        window = f"-{int(inactive_days)} days"
        async with conn:
            row = await (
                await conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE
                            WHEN has_started = 1 AND is_banned = 0
                                 AND last_activity IS NOT NULL
                                 AND last_activity >= datetime('now', ?)
                            THEN 1 ELSE 0 END) AS active,
                        SUM(CASE
                            WHEN has_started = 1 AND is_banned = 0
                                 AND (last_activity IS NULL OR last_activity < datetime('now', ?))
                            THEN 1 ELSE 0 END) AS inactive,
                        SUM(CASE WHEN is_banned = 1 THEN 1 ELSE 0 END) AS blacklisted,
                        SUM(CASE WHEN has_started = 0 THEN 1 ELSE 0 END) AS left_count
                    FROM users
                    """,
                    (window, window),
                )
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "inactive": int(row["inactive"] or 0),
            "blacklisted": int(row["blacklisted"] or 0),
            "left": int(row["left_count"] or 0),
        }

    async def get_user(self, telegram_id: int) -> User | None:
        cached = self._user_cache.get(telegram_id)
        if cached is not None:
            return cached
        conn = await self._conn()
        async with conn:
            row = await (await conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))).fetchone()
        return None if row is None else self._cache_user(self._to_user(row))

    async def list_eligible_recipients(self, sender_id: int) -> list[User]:
        users = await self.list_users()
        await self._ensure_blocks_cache()
        started = 0
        banned = 0
        sender = 0
        blocked = 0
        recipients = [
            user
            for user in users
            if self._is_eligible_recipient(user, sender_id)
        ]
        for user in users:
            if user.telegram_id == sender_id:
                sender += 1
                continue
            if not user.has_started:
                continue
            started += 1
            if user.is_banned:
                banned += 1
                continue
            if (
                not user.is_moderator
                and not user.is_admin
                and sender_id in self._blocks_by_blocker.get(user.telegram_id, set())
            ):
                blocked += 1
        logger.debug(
            "Recipient eligibility sender_id=%s users=%s started_excluding_sender=%s banned=%s blocked=%s eligible=%s",
            sender_id,
            len(users),
            started,
            banned,
            blocked,
            len(recipients),
        )
        return sorted(
            recipients,
            key=lambda user: self._sort_activity(user.last_activity),
            reverse=True,
        )

    def _is_eligible_recipient(self, user: User, sender_id: int) -> bool:
        if not user.has_started:
            return False
        if user.is_banned:
            return False
        if user.telegram_id == sender_id:
            return False
        if user.is_moderator or user.is_admin:
            return True
        return sender_id not in self._blocks_by_blocker.get(user.telegram_id, set())

    async def _ensure_blocks_cache(self) -> None:
        if self._blocks_cache_loaded:
            return
        conn = await self._conn()
        async with conn:
            rows = await (await conn.execute("SELECT blocker_id, blocked_id FROM blocks")).fetchall()
        blocks: dict[int, set[int]] = {}
        for row in rows:
            blocks.setdefault(int(row["blocker_id"]), set()).add(int(row["blocked_id"]))
        self._blocks_by_blocker = blocks
        self._blocks_cache_loaded = True

    @staticmethod
    def _sort_activity(raw: str | None) -> datetime:
        if raw is None:
            return datetime.fromtimestamp(0, timezone.utc)
        try:
            return as_utc(raw)
        except ValueError:
            return datetime.fromtimestamp(0, timezone.utc)

    async def create_message(
        self,
        sender_id: int,
        content_type: str,
        text_content: str | None,
        media_file_id: str | None,
        media_kind: str | None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        thumbnail_file_id: str | None = None,
        is_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_message_id: int | None = None,
        media_hash: str | None = None,
        media_hash_first_seen_at: str | None = None,
        sticker_set_name: str | None = None,
    ) -> int:
        message_id = self.transient.next_message_id()
        self.transient.messages[message_id] = {
            "id": message_id,
            "sender_id": sender_id,
            "content_type": content_type,
            "text_content": text_content,
            "media_file_id": media_file_id,
            "media_kind": media_kind,
            "thumbnail_file_id": thumbnail_file_id,
            "is_forward": 1 if is_forward else 0,
            "forward_from_chat_id": forward_from_chat_id,
            "forward_message_id": forward_message_id,
            "media_hash": media_hash,
            "media_hash_first_seen_at": media_hash_first_seen_at,
            "sticker_set_name": sticker_set_name,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "reply_to_message_id": reply_to_message_id,
            "parse_mode": parse_mode,
            "tag": "PENDING",
            "tag_reason": None,
            "is_deleted": 0,
            "deletion_reason": None,
            "tombstone_mod_message_id": None,
            "punishment_confirmed": 0,
            "removed_for_mods": 0,
            "reverted": 0,
            "created_at": self.transient.iso_now(),
        }
        if source_chat_id is not None and source_message_id is not None:
            self.transient.source_message_index[(int(source_chat_id), int(source_message_id))] = message_id
        return message_id

    async def latest_message_by_sender(self, sender_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        rows = [
            row for row in self.transient.messages.values()
            if int(row["sender_id"]) == sender_id and not bool(row["is_deleted"])
        ]
        return max(rows, key=lambda r: int(r["id"]), default=None)

    async def message_by_source(self, source_chat_id: int, source_message_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        message_id = self.transient.source_message_index.get((source_chat_id, source_message_id))
        if message_id is None:
            return None
        row = self.transient.messages.get(message_id)
        if row is None or bool(row["is_deleted"]):
            return None
        return row

    async def set_message_tag(self, message_id: int, tag: str, reason: str | None) -> None:
        row = self.transient.messages.get(message_id)
        if row is not None:
            row["tag"] = tag
            row["tag_reason"] = reason

    async def block_sticker_set(self, set_name: str, blocked_by: int, reason: str | None = None) -> None:
        normalized = self._normalize_sticker_set_name(set_name)
        if not normalized:
            return
        conn = await self._conn()
        async with conn:
            await conn.execute(
                """
                INSERT INTO blocked_sticker_sets (set_name, blocked_by, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(set_name) DO UPDATE SET
                    blocked_by = excluded.blocked_by,
                    reason = excluded.reason,
                    created_at = CURRENT_TIMESTAMP
                """,
                (normalized, blocked_by, reason),
            )
            await conn.commit()
        if self._blocked_sticker_sets_loaded:
            self._blocked_sticker_sets.add(normalized)

    async def is_sticker_set_blocked(self, set_name: str | None) -> bool:
        normalized = self._normalize_sticker_set_name(set_name)
        if not normalized:
            return False
        await self._ensure_blocked_sticker_sets_cache()
        return normalized in self._blocked_sticker_sets

    async def _ensure_blocked_sticker_sets_cache(self) -> None:
        if self._blocked_sticker_sets_loaded:
            return
        conn = await self._conn()
        async with conn:
            rows = await (await conn.execute("SELECT set_name FROM blocked_sticker_sets")).fetchall()
        self._blocked_sticker_sets = {str(row["set_name"]).casefold() for row in rows}
        self._blocked_sticker_sets_loaded = True

    @staticmethod
    def _normalize_sticker_set_name(set_name: str | None) -> str:
        return (set_name or "").strip().casefold()

    async def recent_media_hashes(self, since_days: int) -> list[str]:
        await self._ensure_media_hash_cache()
        if since_days < 0:
            return list(self._media_hash_first_seen)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(since_days))
        return [
            hash_value
            for hash_value, latest_seen in self._media_hash_latest_seen.items()
            if self._parse_cached_time(latest_seen) >= cutoff
        ]

    async def add_media_hash(self, hash_value: str) -> None:
        await self._ensure_media_hash_cache()
        created_at = datetime.now(timezone.utc).isoformat()
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "INSERT INTO media_hashes (hash, created_at) VALUES (?, ?)",
                (hash_value, created_at),
            )
            await conn.commit()
        self._media_hash_first_seen.setdefault(hash_value, created_at)
        self._media_hash_latest_seen[hash_value] = created_at

    async def first_media_hash_seen_at(self, hash_value: str) -> str | None:
        await self._ensure_media_hash_cache()
        return self._media_hash_first_seen.get(hash_value)

    async def media_hash_seen_within(self, hash_value: str, days: int) -> bool:
        await self._ensure_media_hash_cache()
        if days < 0:
            return hash_value in self._media_hash_first_seen
        latest_seen = self._media_hash_latest_seen.get(hash_value)
        if latest_seen is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        return self._parse_cached_time(latest_seen) >= cutoff

    async def _ensure_media_hash_cache(self) -> None:
        if self._media_hash_cache_loaded:
            return
        conn = await self._conn()
        async with conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT hash, MIN(created_at) AS first_seen, MAX(created_at) AS latest_seen
                    FROM media_hashes
                    GROUP BY hash
                    """
                )
            ).fetchall()
        self._media_hash_first_seen = {
            str(row["hash"]): str(row["first_seen"]) for row in rows
        }
        self._media_hash_latest_seen = {
            str(row["hash"]): str(row["latest_seen"]) for row in rows
        }
        self._media_hash_cache_loaded = True

    @staticmethod
    def _parse_cached_time(raw: str) -> datetime:
        try:
            return as_utc(raw)
        except ValueError:
            return datetime.fromtimestamp(0, timezone.utc)

    async def set_message_media_hash(self, message_id: int, hash_value: str, first_seen_at: str | None) -> None:
        row = self.transient.messages.get(message_id)
        if row is not None:
            row["media_hash"] = hash_value
            row["media_hash_first_seen_at"] = first_seen_at

    async def prune_media_hashes(self, older_than_days: int) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "DELETE FROM media_hashes WHERE created_at < datetime('now', ?)",
                (f"-{int(older_than_days)} days",),
            )
            await conn.commit()

    async def get_message(self, message_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        return self.transient.messages.get(message_id)

    async def set_message_deleted(self, message_id: int, reason: str) -> None:
        row = self.transient.messages.get(message_id)
        if row is not None:
            row["is_deleted"] = 1
            row["deletion_reason"] = reason
            if row.get("source_chat_id") is not None and row.get("source_message_id") is not None:
                self.transient.source_message_index.pop(
                    (int(row["source_chat_id"]), int(row["source_message_id"])),
                    None,
                )
            self.transient.removals.append(
                {
                    "message_id": message_id,
                    "sender_id": int(row["sender_id"]),
                    "reason": reason,
                    "created_at": self.transient.iso_now(),
                }
            )

    async def set_message_tombstone_mod_message(self, message_id: int, telegram_message_id: int) -> None:
        row = self.transient.messages.get(message_id)
        if row is not None:
            row["tombstone_mod_message_id"] = telegram_message_id

    async def set_message_moderation_state(
        self,
        message_id: int,
        *,
        punishment_confirmed: bool | None = None,
        removed_for_mods: bool | None = None,
        reverted: bool | None = None,
    ) -> None:
        row = self.transient.messages.get(message_id)
        if row is None:
            return
        if punishment_confirmed is not None:
            row["punishment_confirmed"] = int(punishment_confirmed)
        if removed_for_mods is not None:
            row["removed_for_mods"] = int(removed_for_mods)
        if reverted is not None:
            row["reverted"] = int(reverted)

    async def update_message_text_content(
        self,
        message_id: int,
        text_content: str | None,
        parse_mode: str | None = None,
    ) -> None:
        row = self.transient.messages.get(message_id)
        if row is not None:
            row["text_content"] = text_content
            row["parse_mode"] = parse_mode

    async def add_delivery(
        self,
        message_id: int,
        recipient_id: int,
        telegram_message_id: int,
        is_blurred: bool = False,
    ) -> None:
        if (recipient_id, telegram_message_id) in self.transient.delivery_by_recipient_message:
            return
        delivery_id = self.transient.next_delivery_id()
        self.transient.deliveries[delivery_id] = {
            "id": delivery_id,
            "message_id": message_id,
            "recipient_id": recipient_id,
            "telegram_message_id": telegram_message_id,
            "is_blurred": 1 if is_blurred else 0,
            "deleted": 0,
            "tombstone_message_id": None,
            "tombstone_kind": None,
            "created_at": self.transient.iso_now(),
        }
        self.transient.delivery_by_recipient_message[(recipient_id, telegram_message_id)] = delivery_id
        self.transient.delivery_ids_by_message.setdefault(message_id, set()).add(delivery_id)
        self.transient.delivery_ids_by_message_recipient.setdefault((message_id, recipient_id), []).append(delivery_id)

    async def list_deliveries_for_message(self, message_id: int) -> list[dict[str, Any]]:
        self.transient.cleanup()
        delivery_ids = self.transient.delivery_ids_by_message.get(message_id, set())
        return [
            row
            for delivery_id in sorted(delivery_ids)
            if (row := self.transient.deliveries.get(delivery_id)) is not None
        ]

    async def add_moderation_note(
        self,
        message_id: int,
        sender_id: int,
        moderator_id: int,
        telegram_message_id: int,
        reason: str,
        note_type: str,
    ) -> int:
        self.transient.moderation_notes.append(
            {
                "id": len(self.transient.moderation_notes) + 1,
                "message_id": message_id,
                "sender_id": sender_id,
                "moderator_id": moderator_id,
                "telegram_message_id": telegram_message_id,
                "reason": reason,
                "note_type": note_type,
                "created_at": self.transient.iso_now(),
            }
        )
        return int(self.transient.moderation_notes[-1]["id"])

    async def list_moderation_notes_for_message(self, message_id: int) -> list[dict[str, Any]]:
        self.transient.cleanup()
        return [
            row for row in self.transient.moderation_notes
            if int(row["message_id"]) == message_id
        ]

    async def add_audit_event(
        self,
        event_type: str,
        actor_id: int | None = None,
        target_user_id: int | None = None,
        message_id: int | None = None,
        details: str | None = None,
    ) -> None:
        return None

    async def list_messages_by_sender(self, sender_id: int) -> list[dict[str, Any]]:
        self.transient.cleanup()
        rows = [
            row for row in self.transient.messages.values()
            if int(row["sender_id"]) == sender_id and not bool(row["is_deleted"])
        ]
        return sorted(rows, key=lambda r: int(r["id"]), reverse=True)

    async def list_neighbor_messages_by_sender(self, sender_id: int, pivot_message_id: int, limit: int) -> list[dict[str, Any]]:
        self.transient.cleanup()
        rows = sorted(
            [
                row for row in self.transient.messages.values()
                if int(row["sender_id"]) == sender_id and not bool(row["is_deleted"]) and int(row["id"]) != pivot_message_id
            ],
            key=lambda r: int(r["id"]),
        )
        before = [
            row for row in self.transient.messages.values()
            if int(row["sender_id"]) == sender_id and not bool(row["is_deleted"]) and int(row["id"]) < pivot_message_id
        ]
        after = [
            row for row in rows
            if int(row["id"]) > pivot_message_id
        ]
        before = sorted(before, key=lambda r: int(r["id"]), reverse=True)[:limit]
        after = after[:limit]
        return sorted(before + after, key=lambda r: int(r["id"]))

    async def sender_by_delivery(self, recipient_id: int, replied_message_id: int) -> tuple[int, int] | None:
        self.transient.cleanup()
        delivery_id = self.transient.delivery_by_recipient_message.get((recipient_id, replied_message_id))
        row = None if delivery_id is None else self.transient.deliveries.get(delivery_id)
        if row is None:
            return None
        message = self.transient.messages.get(int(row["message_id"]))
        if message is None:
            return None
        return int(row["message_id"]), int(message["sender_id"])

    async def delivery_message_for_recipient(self, message_id: int, recipient_id: int) -> int | None:
        self.transient.cleanup()
        message = self.transient.messages.get(message_id)
        if (
            message is not None
            and int(message["sender_id"]) == recipient_id
            and message.get("source_chat_id") == recipient_id
            and message.get("source_message_id") is not None
        ):
            return int(message["source_message_id"])
        rows = [
            row
            for delivery_id in self.transient.delivery_ids_by_message_recipient.get((message_id, recipient_id), [])
            if (row := self.transient.deliveries.get(delivery_id)) is not None and not bool(row["deleted"])
        ]
        if not rows:
            return None
        return int(max(rows, key=lambda r: int(r["id"]))["telegram_message_id"])

    async def delivery_for_recipient(self, message_id: int, recipient_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        rows = [
            row
            for delivery_id in self.transient.delivery_ids_by_message_recipient.get((message_id, recipient_id), [])
            if (row := self.transient.deliveries.get(delivery_id)) is not None
        ]
        if not rows:
            return None
        return dict(max(rows, key=lambda r: int(r["id"])))

    async def delivery_or_tombstone_message_for_recipient(self, message_id: int, recipient_id: int) -> int | None:
        self.transient.cleanup()
        direct = await self.delivery_message_for_recipient(message_id, recipient_id)
        if direct is not None:
            return direct
        rows = [
            row
            for delivery_id in self.transient.delivery_ids_by_message_recipient.get((message_id, recipient_id), [])
            if (row := self.transient.deliveries.get(delivery_id)) is not None
        ]
        if not rows:
            return None
        row = max(rows, key=lambda r: int(r["id"]))
        if row.get("tombstone_message_id") is not None:
            return int(row["tombstone_message_id"])
        return int(row["telegram_message_id"])

    async def get_about(self) -> str:
        conn = await self._conn()
        async with conn:
            row = await (await conn.execute("SELECT message FROM about_state WHERE id = 1")).fetchone()
        return str(row["message"])

    async def set_about(self, text: str) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE about_state SET message = ? WHERE id = 1", (text,))
            await conn.commit()

    async def set_confirmation_enabled(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET confirmation_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_votes_enabled(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET votes_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_vote_buttons_enabled(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET vote_buttons_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_about_seen(self, telegram_id: int, seen: bool = True) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET about_seen = ? WHERE telegram_id = ?",
                (1 if seen else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_hide_potentially_unwanted(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET hide_potentially_unwanted = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_filter_duplicates(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET filter_duplicates = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_fights_enabled(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET fights_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_sign_enabled(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET sign_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_tripcode(self, telegram_id: int, enabled: bool, name: str | None, hash_value: str | None) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                """
                UPDATE users
                SET tripcode_enabled = ?, tripcode_name = ?, tripcode_hash = ?
                WHERE telegram_id = ?
                """,
                (1 if enabled else 0, name, hash_value, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_moderator(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "UPDATE users SET is_moderator = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def set_banned(self, telegram_id: int, enabled: bool) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE users SET is_banned = ? WHERE telegram_id = ?", (1 if enabled else 0, telegram_id))
            await conn.commit()
        await self._refresh_user_cache(telegram_id)

    async def list_banned_users(self) -> list[User]:
        conn = await self._conn()
        async with conn:
            rows = await (await conn.execute("SELECT * FROM users WHERE is_banned = 1")).fetchall()
        return [self._to_user(r) for r in rows]

    async def add_block(self, blocker_id: int, blocked_id: int) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
                (blocker_id, blocked_id),
            )
            await conn.commit()
        if self._blocks_cache_loaded:
            self._blocks_by_blocker.setdefault(blocker_id, set()).add(blocked_id)

    async def remove_last_block(self, blocker_id: int) -> int | None:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT blocked_id FROM blocks WHERE blocker_id = ? ORDER BY created_at DESC LIMIT 1",
                    (blocker_id,),
                )
            ).fetchone()
            if row is None:
                return None
            blocked_id = int(row["blocked_id"])
            await conn.execute(
                "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                (blocker_id, blocked_id),
            )
            await conn.commit()
        if self._blocks_cache_loaded:
            blocked = self._blocks_by_blocker.get(blocker_id)
            if blocked is not None:
                blocked.discard(blocked_id)
                if not blocked:
                    self._blocks_by_blocker.pop(blocker_id, None)
        return blocked_id

    async def set_cooldown(self, user_id: int, until_at_iso: str, reason: str, applied_by: int) -> None:
        now = datetime.now(timezone.utc)
        requested_until = as_utc(until_at_iso)
        duration = max(timedelta(0), requested_until - now)
        conn = await self._conn()
        async with conn:
            existing = await (
                await conn.execute(
                    "SELECT until_at FROM cooldowns WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
            final_until = requested_until
            if existing is not None:
                try:
                    current_until = as_utc(str(existing["until_at"]))
                    if current_until > now:
                        final_until = current_until + duration
                except ValueError:
                    final_until = requested_until
            final_until_iso = final_until.isoformat()
            await conn.execute(
                """
                INSERT INTO cooldowns (user_id, until_at, reason, applied_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET until_at = excluded.until_at, reason = excluded.reason, applied_by = excluded.applied_by
                """,
                (user_id, final_until_iso, reason, applied_by),
            )
            await conn.execute(
                "INSERT INTO cooldown_history (user_id, until_at, reason, applied_by) VALUES (?, ?, ?, ?)",
                (user_id, final_until_iso, reason, applied_by),
            )
            await conn.commit()

    async def clear_cooldown(self, user_id: int) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("DELETE FROM cooldowns WHERE user_id = ?", (user_id,))
            await conn.commit()

    async def get_active_cooldown(self, user_id: int) -> aiosqlite.Row | None:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM cooldowns WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
        if row is None:
            return None
        try:
            until = as_utc(str(row["until_at"]))
            if until <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        return row

    async def list_cooldown_history(self) -> list[aiosqlite.Row]:
        conn = await self._conn()
        async with conn:
            rows = await (
                await conn.execute("SELECT * FROM cooldown_history ORDER BY created_at DESC LIMIT 200")
            ).fetchall()
        return rows

    async def list_active_cooldowns(self) -> list[aiosqlite.Row]:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self._conn()
        async with conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM cooldowns WHERE until_at > ? ORDER BY until_at ASC",
                    (now,),
                )
            ).fetchall()
        return rows

    async def add_warning(self, user_id: int, warned_by: int, message: str) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE users SET warning_count = warning_count + 1 WHERE telegram_id = ?", (user_id,))
            await conn.commit()
        await self._refresh_user_cache(user_id)

    async def warning_count(self, user_id: int) -> int:
        conn = await self._conn()
        async with conn:
            row = await (await conn.execute("SELECT warning_count FROM users WHERE telegram_id = ?", (user_id,))).fetchone()
        return 0 if row is None else int(row["warning_count"] or 0)

    async def get_received_vote_counts(self, sender_id: int) -> tuple[int, int]:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT upvotes_received, downvotes_received FROM users WHERE telegram_id = ?",
                    (sender_id,),
                )
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["upvotes_received"] or 0), int(row["downvotes_received"] or 0)

    async def increment_received_vote_count(self, sender_id: int, vote_type: str) -> None:
        col = "upvotes_received" if vote_type == "upvote" else "downvotes_received"
        conn = await self._conn()
        async with conn:
            await conn.execute(f"UPDATE users SET {col} = {col} + 1 WHERE telegram_id = ?", (sender_id,))
            await conn.commit()
        await self._refresh_user_cache(sender_id)

    async def adjust_credits(self, user_id: int, amount: float, reason: str) -> float:
        amount = round_credit(amount)
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE users SET credits = ROUND(credits + ?, 2) WHERE telegram_id = ?", (amount, user_id))
            await conn.execute(
                "INSERT INTO credit_transactions (user_id, amount, reason) VALUES (?, ?, ?)",
                (user_id, amount, reason),
            )
            row = await (await conn.execute("SELECT credits FROM users WHERE telegram_id = ?", (user_id,))).fetchone()
            await conn.commit()
        await self._refresh_user_cache(user_id)
        return round_credit(float(row["credits"]))

    async def positive_credits_today(self, user_id: int, reason: str) -> float:
        start_of_day = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0.0) AS total
                    FROM credit_transactions
                    WHERE user_id = ?
                      AND reason = ?
                      AND amount > 0
                      AND created_at >= ?
                    """,
                    (user_id, reason, start_of_day),
                )
            ).fetchone()
        return float(row["total"] or 0.0)

    async def credits_transfer(self, sender_id: int, target_id: int, amount: float, allow_negative_sender: bool = False) -> bool:
        amount = round_credit(amount)
        if amount == 0:
            return False
        conn = await self._conn()
        async with conn:
            sender_row = await (await conn.execute("SELECT credits FROM users WHERE telegram_id = ?", (sender_id,))).fetchone()
            target_row = await (await conn.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (target_id,))).fetchone()
            if sender_row is None:
                return False
            if target_row is None:
                return False
            if not allow_negative_sender and float(sender_row["credits"]) < amount:
                return False
            await conn.execute("UPDATE users SET credits = ROUND(credits - ?, 2) WHERE telegram_id = ?", (amount, sender_id))
            await conn.execute("UPDATE users SET credits = ROUND(credits + ?, 2) WHERE telegram_id = ?", (amount, target_id))
            await conn.execute(
                "INSERT INTO credit_transactions (user_id, amount, reason) VALUES (?, ?, ?)",
                (sender_id, -amount, f"transfer_to:{target_id}"),
            )
            await conn.execute(
                "INSERT INTO credit_transactions (user_id, amount, reason) VALUES (?, ?, ?)",
                (target_id, amount, f"transfer_from:{sender_id}"),
            )
            await conn.commit()
        await self._refresh_user_cache(sender_id)
        await self._refresh_user_cache(target_id)
        return True

    async def list_top_credits(self, since_days: int | None, limit: int = 10) -> list[aiosqlite.Row]:
        conn = await self._conn()
        async with conn:
            if since_days is None:
                rows = await (
                    await conn.execute(
                        """
                        SELECT telegram_id, username, tripcode_enabled, tripcode_name, tripcode_hash, credits
                        FROM users
                        WHERE has_started = 1 AND is_banned = 0
                        ORDER BY credits DESC
                        LIMIT ?
                        """,
                        (limit,),
                    )
                ).fetchall()
            else:
                rows = await (
                    await conn.execute(
                        """
                        SELECT u.telegram_id, u.username, u.tripcode_enabled, u.tripcode_name, u.tripcode_hash,
                               COALESCE(SUM(t.amount), 0.0) AS earned
                        FROM users u
                        LEFT JOIN credit_transactions t ON t.user_id = u.telegram_id
                            AND t.amount > 0
                            AND t.created_at >= datetime('now', ?)
                        WHERE u.has_started = 1 AND u.is_banned = 0
                        GROUP BY u.telegram_id
                        ORDER BY earned DESC
                        LIMIT ?
                        """,
                        (f"-{since_days} days", limit),
                    )
                ).fetchall()
        return rows

    async def get_credit_distribution(self) -> tuple[float, float, float]:
        conn = await self._conn()
        async with conn:
            rows = await (await conn.execute("SELECT credits FROM users WHERE has_started = 1 AND is_banned = 0 ORDER BY credits")).fetchall()
        if not rows:
            return 0.0, 0.0, 0.0
        vals = [float(r["credits"]) for r in rows]
        min_v = vals[0]
        max_v = vals[-1]
        n = len(vals)
        median = vals[n //
                      2] if n % 2 == 1 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        return min_v, median, max_v

    async def credit_cutoff_for_top_fraction(self, fraction: float) -> float:
        conn = await self._conn()
        async with conn:
            rows = await (
                await conn.execute(
                    "SELECT credits FROM users WHERE has_started = 1 AND is_banned = 0 ORDER BY credits DESC"
                )
            ).fetchall()
        if not rows:
            return 0.0
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction <= 0:
            return float(rows[0]["credits"])
        index = max(0, min(len(rows) - 1, math.ceil(len(rows) * fraction) - 1))
        return float(rows[index]["credits"])

    async def current_supply(self) -> float:
        conn = await self._conn()
        async with conn:
            row = await (await conn.execute("SELECT COALESCE(SUM(credits), 0.0) AS s FROM users WHERE is_banned = 0")).fetchone()
        return float(row["s"])

    async def apply_daily_tax_once(self, user_id: int, tax_date: str, amount: float) -> bool:
        amount = round_credit(amount)
        if amount <= 0:
            return False
        conn = await self._conn()
        async with conn:
            try:
                await conn.execute(
                    "INSERT INTO credit_tax_runs (user_id, tax_date, amount) VALUES (?, ?, ?)",
                    (user_id, tax_date, amount),
                )
            except aiosqlite.IntegrityError:
                return False
            await conn.execute("UPDATE users SET credits = ROUND(credits - ?, 2) WHERE telegram_id = ?", (amount, user_id))
            await conn.execute(
                "INSERT INTO credit_transactions (user_id, amount, reason) VALUES (?, ?, ?)",
                (user_id, -amount, f"daily_tax:{tax_date}"),
            )
            await conn.commit()
        await self._refresh_user_cache(user_id)
        return True

    async def mark_delivery_tombstoned(
        self,
        delivery_id: int,
        tombstone_message_id: int | None,
        tombstone_kind: str,
    ) -> None:
        row = self.transient.deliveries.get(delivery_id)
        if row is not None:
            row["deleted"] = 1
            row["tombstone_message_id"] = tombstone_message_id
            row["tombstone_kind"] = tombstone_kind
            if tombstone_message_id is not None:
                self.transient.delivery_by_recipient_message[
                    (int(row["recipient_id"]), int(tombstone_message_id))
                ] = delivery_id
        return True

    async def net_issuance_since_days(self, days: int) -> float:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) AS s FROM credit_transactions WHERE created_at >= datetime('now', ?)",
                    (f"-{days} days",),
                )
            ).fetchone()
        return float(row["s"])

    async def add_vote(self, message_id: int, voter_id: int, vote_type: str, cost: float) -> bool:
        self.transient.cleanup()
        key = (message_id, voter_id)
        typed_key = (message_id, voter_id, vote_type)
        if key in self.transient.votes or typed_key in self.transient.typed_votes:
            return False
        self.transient.votes.add(key)
        self.transient.typed_votes.add(typed_key)
        return True

    async def has_any_vote(self, message_id: int, voter_id: int) -> bool:
        self.transient.cleanup()
        return (message_id, voter_id) in self.transient.votes

    async def add_remove_vote(self, message_id: int, voter_id: int) -> bool:
        self.transient.cleanup()
        key = (message_id, voter_id)
        if key in self.transient.remove_votes:
            return False
        self.transient.remove_votes[key] = self.transient.iso_now()
        return True

    async def count_user_remove_votes_in_window(self, voter_id: int, seconds: int) -> int:
        self.transient.cleanup()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        total = 0
        for (_, uid), created_at in self.transient.remove_votes.items():
            if uid != voter_id:
                continue
            try:
                dt = as_utc(created_at)
            except ValueError:
                continue
            if dt >= cutoff:
                total += 1
        return total

    async def user_last_remove_vote_at(self, voter_id: int) -> str | None:
        self.transient.cleanup()
        times = [created_at for (
            _, uid), created_at in self.transient.remove_votes.items() if uid == voter_id]
        return max(times) if times else None

    async def count_global_removals_in_window(self, seconds: int) -> int:
        self.transient.cleanup()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        total = 0
        for row in self.transient.removals:
            if row.get("reason") != "community vote threshold reached":
                continue
            try:
                dt = as_utc(str(row["created_at"]))
            except ValueError:
                continue
            if dt >= cutoff:
                total += 1
        return total

    async def count_remove_votes(self, message_id: int) -> int:
        self.transient.cleanup()
        return sum(1 for mid, _ in self.transient.remove_votes if mid == message_id)

    async def list_remove_voters(self, message_id: int) -> list[int]:
        self.transient.cleanup()
        return [uid for mid, uid in self.transient.remove_votes if mid == message_id]

    async def get_vote_counts(self, message_id: int) -> tuple[int, int]:
        self.transient.cleanup()
        up = sum(1 for mid, _, typ in self.transient.typed_votes if mid ==
                 message_id and typ == "upvote")
        down = sum(1 for mid, _, typ in self.transient.typed_votes if mid ==
                   message_id and typ == "downvote")
        return up, down

    async def get_downvote_state(self, user_id: int) -> tuple[float, str | None]:
        conn = await self._conn()
        async with conn:
            row = await (
                await conn.execute(
                    "SELECT streak, last_downvote_at FROM user_downvote_state WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
        if row is None:
            return 0.0, None
        return float(row["streak"] or 0.0), row["last_downvote_at"]

    async def set_downvote_state(self, user_id: int, streak: float, last_downvote_at_iso: str) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                """
                INSERT INTO user_downvote_state (user_id, streak, last_downvote_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET streak = excluded.streak, last_downvote_at = excluded.last_downvote_at
                """,
                (user_id, streak, last_downvote_at_iso),
            )
            await conn.commit()

    async def list_mod_and_admin_users(self) -> list[User]:
        conn = await self._conn()
        async with conn:
            rows = await (
                await conn.execute("SELECT * FROM users WHERE is_moderator = 1 OR is_admin = 1")
            ).fetchall()
        return [self._to_user(r) for r in rows]

    async def create_whisper(self, sender_id: int, recipient_id: int, text_content: str, is_modwhisper: bool) -> int:
        whisper_id = self.transient.next_whisper_id()
        self.transient.whispers[whisper_id] = {
            "id": whisper_id,
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "text_content": text_content,
            "is_modwhisper": 1 if is_modwhisper else 0,
            "created_at": self.transient.iso_now(),
        }
        return whisper_id

    async def add_whisper_delivery(self, whisper_id: int, recipient_id: int, telegram_message_id: int) -> None:
        if any(
            int(row["recipient_id"]) == recipient_id and int(
                row["telegram_message_id"]) == telegram_message_id
            for row in self.transient.whisper_deliveries.values()
        ):
            return
        delivery_id = self.transient.next_delivery_id()
        self.transient.whisper_deliveries[delivery_id] = {
            "id": delivery_id,
            "whisper_id": whisper_id,
            "recipient_id": recipient_id,
            "telegram_message_id": telegram_message_id,
            "created_at": self.transient.iso_now(),
        }

    async def whisper_sender_by_reply(self, recipient_id: int, replied_message_id: int) -> int | None:
        row = await self.whisper_context_by_reply(recipient_id, replied_message_id)
        return None if row is None else int(row["sender_id"])

    async def whisper_context_by_reply(self, recipient_id: int, replied_message_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        deliveries = [
            row for row in self.transient.whisper_deliveries.values()
            if int(row["recipient_id"]) == recipient_id and int(row["telegram_message_id"]) == replied_message_id
        ]
        if not deliveries:
            return None
        delivery = max(deliveries, key=lambda r: int(r["id"]))
        return self.transient.whispers.get(int(delivery["whisper_id"]))

    async def whisper_delivery_message_id(self, whisper_id: int, recipient_id: int) -> int | None:
        self.transient.cleanup()
        rows = [
            row for row in self.transient.whisper_deliveries.values()
            if int(row["whisper_id"]) == whisper_id and int(row["recipient_id"]) == recipient_id
        ]
        if not rows:
            return None
        return int(max(rows, key=lambda r: int(r["id"]))["telegram_message_id"])

    async def list_whisper_deliveries(self, whisper_id: int) -> list[dict[str, Any]]:
        self.transient.cleanup()
        return [
            dict(row) for row in self.transient.whisper_deliveries.values()
            if int(row["whisper_id"]) == whisper_id
        ]

    async def create_fight_request(
        self,
        initiator_id: int,
        recipient_id: int,
        stake: float,
        fee: float,
        expires_at: str,
        initiator_message_id: int | None = None,
    ) -> int:
        fight_id = self.transient.next_fight_id()
        self.transient.fights[fight_id] = {
            "id": fight_id,
            "initiator_id": initiator_id,
            "recipient_id": recipient_id,
            "stake": stake,
            "fee": fee,
            "status": "PENDING",
            "created_at": self.transient.iso_now(),
            "expires_at": expires_at,
            "initiator_message_id": initiator_message_id,
        }
        return fight_id

    async def latest_fight_by_initiator(self, initiator_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        rows = [row for row in self.transient.fights.values() if int(
            row["initiator_id"]) == initiator_id]
        return max(rows, key=lambda r: int(r["id"]), default=None)

    async def get_fight_request(self, fight_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        return self.transient.fights.get(fight_id)

    async def set_fight_status(self, fight_id: int, status: str) -> None:
        row = self.transient.fights.get(fight_id)
        if row is not None:
            row["status"] = status

    async def upsert_invite(self, inviter_id: int, invite_code: str) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute(
                "INSERT OR REPLACE INTO invites (invite_code, inviter_id, uses) VALUES (?, ?, COALESCE((SELECT uses FROM invites WHERE invite_code = ?),0))",
                (invite_code, inviter_id, invite_code),
            )
            await conn.commit()

    async def invite_by_inviter(self, inviter_id: int) -> aiosqlite.Row | None:
        conn = await self._conn()
        async with conn:
            return await (
                await conn.execute(
                    "SELECT * FROM invites WHERE inviter_id = ? ORDER BY rowid DESC LIMIT 1",
                    (inviter_id,),
                )
            ).fetchone()

    async def invite_by_code(self, code: str) -> aiosqlite.Row | None:
        conn = await self._conn()
        async with conn:
            return await (await conn.execute("SELECT * FROM invites WHERE invite_code = ?", (code,))).fetchone()

    async def increment_invite_use(self, code: str) -> None:
        conn = await self._conn()
        async with conn:
            await conn.execute("UPDATE invites SET uses = uses + 1 WHERE invite_code = ?", (code,))
            await conn.commit()

    async def redeem_invite_once(self, code: str, invitee_id: int) -> bool:
        conn = await self._conn()
        async with conn:
            existing = await (
                await conn.execute(
                    "SELECT 1 FROM invite_redemptions WHERE invitee_id = ? LIMIT 1",
                    (invitee_id,),
                )
            ).fetchone()
            if existing is not None:
                return False
            try:
                await conn.execute(
                    "INSERT INTO invite_redemptions (invite_code, invitee_id) VALUES (?, ?)",
                    (code, invitee_id),
                )
            except aiosqlite.IntegrityError:
                return False
            await conn.execute("UPDATE invites SET uses = uses + 1 WHERE invite_code = ?", (code,))
            await conn.commit()
        return True

    async def _recent_count(self, telegram_id: int, window_seconds: int) -> int:
        self.transient.cleanup()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        total = 0
        for row in self.transient.messages.values():
            if int(row["sender_id"]) != telegram_id:
                continue
            try:
                created = as_utc(str(row["created_at"]))
            except ValueError:
                continue
            if created >= cutoff:
                total += 1
        return total

    async def recent_message_count(self, telegram_id: int, window_seconds: int) -> int:
        return await self._recent_count(telegram_id, window_seconds)

    async def get_sauce_cache(self, message_id: int) -> dict[str, Any] | None:
        self.transient.cleanup()
        return self.transient.sauce_cache.get(message_id)

    async def set_sauce_cache(self, message_id: int, result: dict[str, Any]) -> None:
        self.transient.cleanup()
        self.transient.sauce_cache[message_id] = result | {"created_at": self.transient.iso_now()}

    async def sauce_usage_count(self, user_id: int, window_seconds: int = 86400) -> int:
        self.transient.cleanup()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        total = 0
        for raw in self.transient.sauce_usage.get(user_id, []):
            try:
                dt = as_utc(raw)
            except ValueError:
                continue
            if dt >= cutoff:
                total += 1
        return total

    async def sauce_total_usage_count(self, window_seconds: int = 86400) -> int:
        self.transient.cleanup()
        total = 0
        for user_id in list(self.transient.sauce_usage):
            total += await self.sauce_usage_count(user_id, window_seconds)
        return total

    async def add_sauce_usage(self, user_id: int) -> None:
        self.transient.cleanup()
        self.transient.sauce_usage.setdefault(user_id, []).append(self.transient.iso_now())

    @staticmethod
    def _to_user(row: aiosqlite.Row) -> User:
        return User(
            telegram_id=int(row["telegram_id"]),
            username=row["username"],
            has_started=bool(row["has_started"]),
            is_banned=bool(row["is_banned"]),
            is_moderator=bool(row["is_moderator"]),
            is_admin=bool(row["is_admin"]),
            created_at=row["created_at"] if "created_at" in row.keys() else None,
            last_activity=row["last_activity"],
            confirmation_enabled=bool(row["confirmation_enabled"]),
            votes_enabled=bool(row["votes_enabled"]
                               ) if "votes_enabled" in row.keys() else True,
            vote_buttons_enabled=bool(
                row["vote_buttons_enabled"]) if "vote_buttons_enabled" in row.keys() else True,
            hide_potentially_unwanted=bool(
                row["hide_potentially_unwanted"]) if "hide_potentially_unwanted" in row.keys() else False,
            filter_duplicates=bool(
                row["filter_duplicates"]) if "filter_duplicates" in row.keys() else False,
            fights_enabled=bool(
                row["fights_enabled"]) if "fights_enabled" in row.keys() else True,
            sign_enabled=bool(row["sign_enabled"]
                              ) if "sign_enabled" in row.keys() else False,
            tripcode_enabled=bool(
                row["tripcode_enabled"]) if "tripcode_enabled" in row.keys() else False,
            tripcode_name=row["tripcode_name"] if "tripcode_name" in row.keys(
            ) else None,
            tripcode_hash=row["tripcode_hash"] if "tripcode_hash" in row.keys(
            ) else None,
            about_seen=bool(row["about_seen"]) if "about_seen" in row.keys() else False,
            credits=round_credit(float(row["credits"])),
        )
