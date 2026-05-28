from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TransientStore:
    ttl_seconds: int = 259200
    _message_id: int = 0
    _delivery_id: int = 0
    _whisper_id: int = 0
    _fight_id: int = 0
    messages: dict[int, dict[str, Any]] = field(default_factory=dict)
    deliveries: dict[int, dict[str, Any]] = field(default_factory=dict)
    whisper_deliveries: dict[int, dict[str, Any]] = field(default_factory=dict)
    whispers: dict[int, dict[str, Any]] = field(default_factory=dict)
    votes: set[tuple[int, int]] = field(default_factory=set)
    typed_votes: set[tuple[int, int, str]] = field(default_factory=set)
    remove_votes: dict[tuple[int, int], str] = field(default_factory=dict)
    removals: list[dict[str, Any]] = field(default_factory=list)
    moderation_notes: list[dict[str, Any]] = field(default_factory=list)
    fights: dict[int, dict[str, Any]] = field(default_factory=dict)
    sauce_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    sauce_usage: dict[int, list[str]] = field(default_factory=dict)
    source_message_index: dict[tuple[int, int], int] = field(default_factory=dict)
    delivery_by_recipient_message: dict[tuple[int, int], int] = field(default_factory=dict)
    delivery_ids_by_message: dict[int, set[int]] = field(default_factory=dict)
    delivery_ids_by_message_recipient: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def cleanup(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.ttl_seconds)

        def expired(row: dict[str, Any]) -> bool:
            raw = str(row.get("created_at") or "")
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return False
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt < cutoff

        expired_messages = {mid for mid, row in self.messages.items() if expired(row)}
        if expired_messages:
            self.messages = {mid: row for mid, row in self.messages.items() if mid not in expired_messages}
            self.deliveries = {
                did: row for did, row in self.deliveries.items() if int(row["message_id"]) not in expired_messages
            }
            self.votes = {(mid, uid) for mid, uid in self.votes if mid not in expired_messages}
            self.typed_votes = {
                (mid, uid, typ) for mid, uid, typ in self.typed_votes if mid not in expired_messages
            }
            self.remove_votes = {
                key: ts for key, ts in self.remove_votes.items() if key[0] not in expired_messages
            }
            self._rebuild_message_delivery_indexes()

        self.whispers = {wid: row for wid, row in self.whispers.items() if not expired(row)}
        self.whisper_deliveries = {
            did: row for did, row in self.whisper_deliveries.items() if int(row["whisper_id"]) in self.whispers
        }
        self.fights = {fid: row for fid, row in self.fights.items() if not expired(row)}
        self.removals = [row for row in self.removals if not expired(row)]
        self.moderation_notes = [row for row in self.moderation_notes if not expired(row)]
        self.sauce_cache = {mid: row for mid, row in self.sauce_cache.items() if not expired(row)}
        cutoff_usage = datetime.now(timezone.utc) - timedelta(hours=24)
        cleaned_usage: dict[int, list[str]] = {}
        for user_id, timestamps in self.sauce_usage.items():
            kept = []
            for raw in timestamps:
                try:
                    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff_usage:
                    kept.append(raw)
            if kept:
                cleaned_usage[user_id] = kept
        self.sauce_usage = cleaned_usage

    def _rebuild_message_delivery_indexes(self) -> None:
        self.source_message_index = {}
        for message_id, row in self.messages.items():
            chat_id = row.get("source_chat_id")
            telegram_message_id = row.get("source_message_id")
            if chat_id is not None and telegram_message_id is not None and not bool(row.get("is_deleted")):
                self.source_message_index[(int(chat_id), int(telegram_message_id))] = int(message_id)

        self.delivery_by_recipient_message = {}
        self.delivery_ids_by_message = {}
        self.delivery_ids_by_message_recipient = {}
        for delivery_id, row in self.deliveries.items():
            recipient_id = int(row["recipient_id"])
            telegram_message_id = int(row["telegram_message_id"])
            message_id = int(row["message_id"])
            self.delivery_by_recipient_message[(recipient_id, telegram_message_id)] = int(delivery_id)
            if row.get("tombstone_message_id") is not None:
                self.delivery_by_recipient_message[(recipient_id, int(row["tombstone_message_id"]))] = int(delivery_id)
            self.delivery_ids_by_message.setdefault(message_id, set()).add(int(delivery_id))
            self.delivery_ids_by_message_recipient.setdefault((message_id, recipient_id), []).append(int(delivery_id))

    def next_message_id(self) -> int:
        self.cleanup()
        self._message_id += 1
        return self._message_id

    def next_delivery_id(self) -> int:
        self.cleanup()
        self._delivery_id += 1
        return self._delivery_id

    def next_whisper_id(self) -> int:
        self.cleanup()
        self._whisper_id += 1
        return self._whisper_id

    def next_fight_id(self) -> int:
        self.cleanup()
        self._fight_id += 1
        return self._fight_id

    def iso_now(self) -> str:
        return _now_iso()
