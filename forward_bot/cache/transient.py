from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from forward_bot.utils import now_utc, random_token, seconds_left


@dataclass(slots=True)
class TransientMessage:
    id: int
    sender_id: int | None
    content_type: str
    text: str | None = None
    media_file_id: str | None = None
    thumbnail_file_id: str | None = None
    media_kind: str | None = None
    mime_type: str | None = None
    sticker_set_name: str | None = None
    is_animated: bool = False
    is_video: bool = False
    source_chat_id: int | None = None
    source_message_id: int | None = None
    reply_to_message_id: int | None = None
    parse_mode: str | None = None
    tag: str = "OK"
    tag_reason: str | None = None
    is_system: bool = False
    urgent: bool = False
    media_hash: str | None = None
    media_hash_first_seen_at: str | None = None
    remove_buttons: bool = False
    deleted: bool = False
    deletion_reason: str | None = None
    punishment_confirmed: bool = False
    removed_for_mods: bool = False
    reverted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class TransientDelivery:
    id: int
    message_id: int
    recipient_id: int
    telegram_message_id: int
    blurred: bool = False
    deleted: bool = False
    tombstone_message_id: int | None = None
    tombstone_kind: str | None = None
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class Whisper:
    id: int
    sender_id: int
    target_id: int
    text: str
    is_modwhisper: bool = False
    reply_to_message_id: int | None = None
    reply_to_whisper_id: int | None = None
    deleted: bool = False
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class WhisperDelivery:
    id: int
    whisper_id: int
    recipient_id: int
    telegram_message_id: int
    deleted: bool = False
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class FightRequest:
    id: int
    sender_id: int
    target_id: int
    stake: float
    fee: float
    matchup: str
    command_message_id: int | None
    target_message_id: int | None = None
    status: str = "pending"
    created_at: datetime = field(default_factory=now_utc)
    expires_at: datetime = field(default_factory=lambda: now_utc() + timedelta(minutes=5))


class TransientStore:
    def __init__(
        self,
        ttl_hours: int = 72,
        sender_metadata_max_size: int = 86400,
        sender_metadata_ttl_seconds: int | None = None,
    ):
        self.ttl = timedelta(hours=ttl_hours)
        self.sender_metadata_max_size = max(1, int(sender_metadata_max_size))
        self.sender_metadata_ttl = (
            self.ttl
            if sender_metadata_ttl_seconds is None
            else timedelta(seconds=max(1, int(sender_metadata_ttl_seconds)))
        )
        self.messages: dict[int, TransientMessage] = {}
        self.deliveries: dict[int, TransientDelivery] = {}
        self.whispers: dict[int, Whisper] = {}
        self.whisper_deliveries: dict[int, WhisperDelivery] = {}
        self.fights: dict[int, FightRequest] = {}
        self.source_index: dict[tuple[int, int], int] = {}
        self.delivery_index: dict[tuple[int, int], int] = {}
        self.whisper_delivery_index: dict[tuple[int, int], int] = {}
        self.message_delivery_index: dict[int, set[int]] = {}
        self.message_recipient_index: dict[tuple[int, int], int] = {}
        self.whisper_delivery_by_whisper_index: dict[int, set[int]] = {}
        self.votes: set[tuple[str, int, int]] = set()
        self.remove_votes: dict[int, set[int]] = {}
        self.remove_vote_times: dict[int, list[datetime]] = {}
        self.global_remove_events: list[datetime] = []
        self.mod_notes: dict[int, list[tuple[int, int]]] = {}
        self.sender_snapshots: dict[int, tuple[datetime, dict[str, Any]]] = {}
        self.confirmations: dict[int, datetime] = {}
        self.retries: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self.sauce_cache: dict[int, tuple[datetime, str]] = {}
        self.sauce_user_usage: dict[tuple[int, str], int] = {}
        self.sauce_global_usage: dict[str, int] = {}
        self.inactive_notified: set[int] = set()
        self.delivery_stats: dict[int, dict[str, int]] = {}
        self._message_ids = itertools.count(1)
        self._delivery_ids = itertools.count(1)
        self._whisper_ids = itertools.count(1)
        self._whisper_delivery_ids = itertools.count(1)
        self._fight_ids = itertools.count(1)

    def add_message(self, **kwargs: Any) -> TransientMessage:
        msg = TransientMessage(id=next(self._message_ids), **kwargs)
        self.messages[msg.id] = msg
        if msg.source_chat_id and msg.source_message_id:
            self.source_index[(msg.source_chat_id, msg.source_message_id)] = msg.id
        return msg

    def get_message(self, message_id: int | None) -> TransientMessage | None:
        if message_id is None:
            return None
        msg = self.messages.get(message_id)
        if not msg or self._expired(msg.created_at):
            return None
        return msg

    def add_delivery(self, message_id: int, recipient_id: int, telegram_message_id: int, *, blurred: bool = False) -> TransientDelivery:
        delivery = TransientDelivery(
            id=next(self._delivery_ids),
            message_id=message_id,
            recipient_id=recipient_id,
            telegram_message_id=telegram_message_id,
            blurred=blurred,
        )
        self.deliveries[delivery.id] = delivery
        self.delivery_index[(recipient_id, telegram_message_id)] = delivery.id
        self.message_delivery_index.setdefault(message_id, set()).add(delivery.id)
        self.message_recipient_index[(message_id, recipient_id)] = delivery.id
        return delivery

    def resolve_delivery(self, recipient_id: int, telegram_message_id: int) -> TransientDelivery | None:
        delivery_id = self.delivery_index.get((recipient_id, telegram_message_id))
        if not delivery_id:
            return None
        delivery = self.deliveries.get(delivery_id)
        if not delivery or self._expired(delivery.created_at):
            return None
        return delivery

    def resolve_source(self, chat_id: int, message_id: int) -> TransientMessage | None:
        return self.get_message(self.source_index.get((chat_id, message_id)))

    def delivery_for_recipient(self, message_id: int, recipient_id: int) -> TransientDelivery | None:
        delivery_id = self.message_recipient_index.get((message_id, recipient_id))
        if delivery_id is None:
            return None
        delivery = self.deliveries.get(delivery_id)
        if not delivery or delivery.deleted or self._expired(delivery.created_at):
            return None
        return delivery

    def deliveries_for_message(self, message_id: int) -> list[TransientDelivery]:
        deliveries: list[TransientDelivery] = []
        for delivery_id in self.message_delivery_index.get(message_id, set()):
            delivery = self.deliveries.get(delivery_id)
            if delivery and not self._expired(delivery.created_at):
                deliveries.append(delivery)
        return deliveries

    def mark_delivery_deleted(self, delivery_id: int, *, tombstone_message_id: int | None = None, kind: str = "deleted") -> None:
        delivery = self.deliveries.get(delivery_id)
        if delivery:
            delivery.deleted = True
            delivery.tombstone_message_id = tombstone_message_id
            delivery.tombstone_kind = kind

    def record_delivery_status(self, message_id: int, status: str) -> None:
        stats = self.delivery_stats.setdefault(message_id, {})
        stats[status] = stats.get(status, 0) + 1

    def add_vote(self, scope: str, subject_id: int, voter_id: int) -> bool:
        key = (scope, subject_id, voter_id)
        if key in self.votes:
            return False
        self.votes.add(key)
        return True

    def add_remove_vote(self, message_id: int, voter_id: int) -> tuple[bool, int]:
        voters = self.remove_votes.setdefault(message_id, set())
        if voter_id in voters:
            return False, len(voters)
        voters.add(voter_id)
        now = now_utc()
        self.remove_vote_times.setdefault(voter_id, []).append(now)
        return True, len(voters)

    def record_global_removal(self) -> None:
        self.global_remove_events.append(now_utc())

    def remove_vote_count(self, message_id: int) -> int:
        return len(self.remove_votes.get(message_id, set()))

    def recent_remove_votes_by_user(self, user_id: int, window_seconds: int) -> int:
        cutoff = now_utc() - timedelta(seconds=window_seconds)
        events = [t for t in self.remove_vote_times.get(user_id, []) if t >= cutoff]
        self.remove_vote_times[user_id] = events
        return len(events)

    def latest_remove_vote_seconds_left(self, user_id: int, cooldown_seconds: int) -> int:
        events = self.remove_vote_times.get(user_id, [])
        if not events:
            return 0
        until = max(events) + timedelta(seconds=cooldown_seconds)
        return seconds_left(until)

    def recent_global_removals(self, window_seconds: int) -> int:
        cutoff = now_utc() - timedelta(seconds=window_seconds)
        self.global_remove_events = [t for t in self.global_remove_events if t >= cutoff]
        return len(self.global_remove_events)

    def add_mod_note(self, message_id: int, recipient_id: int, telegram_message_id: int) -> None:
        self.mod_notes.setdefault(message_id, []).append((recipient_id, telegram_message_id))

    def set_sender_snapshot(self, message_id: int, snapshot: dict[str, Any]) -> None:
        self.sender_snapshots[message_id] = (now_utc(), snapshot)
        if len(self.sender_snapshots) > self.sender_metadata_max_size:
            oldest = min(self.sender_snapshots.items(), key=lambda item: item[1][0])[0]
            self.sender_snapshots.pop(oldest, None)

    def get_sender_snapshot(self, message_id: int) -> dict[str, Any] | None:
        item = self.sender_snapshots.get(message_id)
        if not item:
            return None
        if self._sender_snapshot_expired(item[0]):
            self.sender_snapshots.pop(message_id, None)
            return None
        return item[1]

    def add_confirmation(self, message_id: int, ttl_seconds: int) -> None:
        self.confirmations[message_id] = now_utc() + timedelta(seconds=ttl_seconds)

    def consume_confirmation(self, message_id: int) -> bool:
        until = self.confirmations.pop(message_id, None)
        return bool(until and until >= now_utc())

    def add_retry(self, payload: dict[str, Any], ttl_seconds: int) -> str:
        token = random_token(10)
        self.retries[token] = (now_utc() + timedelta(seconds=ttl_seconds), payload)
        return token

    def consume_retry(self, token: str) -> dict[str, Any] | None:
        item = self.retries.pop(token, None)
        if not item:
            return None
        until, payload = item
        return payload if until >= now_utc() else None

    def add_whisper(self, **kwargs: Any) -> Whisper:
        whisper = Whisper(id=next(self._whisper_ids), **kwargs)
        self.whispers[whisper.id] = whisper
        return whisper

    def add_whisper_delivery(self, whisper_id: int, recipient_id: int, telegram_message_id: int) -> WhisperDelivery:
        delivery = WhisperDelivery(next(self._whisper_delivery_ids), whisper_id, recipient_id, telegram_message_id)
        self.whisper_deliveries[delivery.id] = delivery
        self.whisper_delivery_index[(recipient_id, telegram_message_id)] = delivery.id
        self.whisper_delivery_by_whisper_index.setdefault(whisper_id, set()).add(delivery.id)
        return delivery

    def resolve_whisper_delivery(self, recipient_id: int, telegram_message_id: int) -> WhisperDelivery | None:
        delivery_id = self.whisper_delivery_index.get((recipient_id, telegram_message_id))
        return self.whisper_deliveries.get(delivery_id) if delivery_id else None

    def deliveries_for_whisper(self, whisper_id: int) -> list[WhisperDelivery]:
        deliveries: list[WhisperDelivery] = []
        for delivery_id in self.whisper_delivery_by_whisper_index.get(whisper_id, set()):
            delivery = self.whisper_deliveries.get(delivery_id)
            if delivery:
                deliveries.append(delivery)
        return deliveries

    def add_fight(self, **kwargs: Any) -> FightRequest:
        fight = FightRequest(id=next(self._fight_ids), **kwargs)
        self.fights[fight.id] = fight
        return fight

    def get_fight(self, fight_id: int) -> FightRequest | None:
        fight = self.fights.get(fight_id)
        if not fight:
            return None
        if fight.status == "pending" and fight.expires_at < now_utc():
            fight.status = "expired"
        return fight

    def expire_due_fights(self) -> list[FightRequest]:
        expired: list[FightRequest] = []
        for fight in self.fights.values():
            if fight.status == "pending" and fight.expires_at < now_utc():
                fight.status = "expired"
                expired.append(fight)
        return expired

    def latest_fight_request_seconds_left(self, sender_id: int, cooldown_seconds: int) -> int:
        latest = max((fight.created_at for fight in self.fights.values() if fight.sender_id == sender_id), default=None)
        if not latest:
            return 0
        return seconds_left(latest + timedelta(seconds=cooldown_seconds))

    def add_sauce_cache(self, message_id: int, result: str) -> None:
        self.sauce_cache[message_id] = (now_utc(), result)

    def get_sauce_cache(self, message_id: int) -> str | None:
        item = self.sauce_cache.get(message_id)
        if not item or self._expired(item[0]):
            return None
        return item[1]

    def record_sauce_usage(self, user_id: int) -> tuple[int, int]:
        day = now_utc().date().isoformat()
        key = (user_id, day)
        self.sauce_user_usage[key] = self.sauce_user_usage.get(key, 0) + 1
        self.sauce_global_usage[day] = self.sauce_global_usage.get(day, 0) + 1
        return self.sauce_user_usage[key], self.sauce_global_usage[day]

    def get_sauce_usage(self, user_id: int) -> tuple[int, int]:
        day = now_utc().date().isoformat()
        return self.sauce_user_usage.get((user_id, day), 0), self.sauce_global_usage.get(day, 0)

    def cleanup(self) -> None:
        self.messages = {k: v for k, v in self.messages.items() if not self._expired(v.created_at)}
        live_message_ids = set(self.messages)
        self.deliveries = {k: v for k, v in self.deliveries.items() if v.message_id in live_message_ids and not self._expired(v.created_at)}
        self.whispers = {k: v for k, v in self.whispers.items() if not self._expired(v.created_at)}
        live_whisper_ids = set(self.whispers)
        self.whisper_deliveries = {k: v for k, v in self.whisper_deliveries.items() if v.whisper_id in live_whisper_ids}
        self.fights = {k: v for k, v in self.fights.items() if not self._expired(v.created_at)}
        for fight in self.fights.values():
            if fight.status == "pending" and fight.expires_at < now_utc():
                fight.status = "expired"
        self.source_index = {
            (m.source_chat_id, m.source_message_id): m.id
            for m in self.messages.values()
            if m.source_chat_id and m.source_message_id
        }
        self.delivery_index = {(d.recipient_id, d.telegram_message_id): d.id for d in self.deliveries.values()}
        self.whisper_delivery_index = {(d.recipient_id, d.telegram_message_id): d.id for d in self.whisper_deliveries.values()}
        self.message_delivery_index = {}
        self.message_recipient_index = {}
        for delivery in self.deliveries.values():
            self.message_delivery_index.setdefault(delivery.message_id, set()).add(delivery.id)
            self.message_recipient_index[(delivery.message_id, delivery.recipient_id)] = delivery.id
        self.whisper_delivery_by_whisper_index = {}
        for delivery in self.whisper_deliveries.values():
            self.whisper_delivery_by_whisper_index.setdefault(delivery.whisper_id, set()).add(delivery.id)
        self.confirmations = {k: v for k, v in self.confirmations.items() if v >= now_utc() and k in live_message_ids}
        self.retries = {k: v for k, v in self.retries.items() if v[0] >= now_utc()}
        self.sauce_cache = {k: v for k, v in self.sauce_cache.items() if not self._expired(v[0]) and k in live_message_ids}
        self.sender_snapshots = {k: v for k, v in self.sender_snapshots.items() if not self._sender_snapshot_expired(v[0]) and k in live_message_ids}
        self.remove_votes = {k: v for k, v in self.remove_votes.items() if k in live_message_ids}
        self.mod_notes = {k: v for k, v in self.mod_notes.items() if k in live_message_ids}
        self.delivery_stats = {k: v for k, v in self.delivery_stats.items() if k in live_message_ids}
        self.votes = {
            vote for vote in self.votes
            if (vote[0] == "msg" and vote[1] in live_message_ids)
            or (vote[0] == "whisper" and vote[1] in live_whisper_ids)
        }
        today = now_utc().date().isoformat()
        self.sauce_user_usage = {k: v for k, v in self.sauce_user_usage.items() if k[1] == today}
        self.sauce_global_usage = {k: v for k, v in self.sauce_global_usage.items() if k == today}

    def _expired(self, created_at: datetime) -> bool:
        return created_at + self.ttl < now_utc()

    def _sender_snapshot_expired(self, created_at: datetime) -> bool:
        return created_at + self.sender_metadata_ttl < now_utc()

    def expired_message_ids(self) -> list[int]:
        return [message_id for message_id, message in self.messages.items() if self._expired(message.created_at)]

    def expired_confirmation_message_ids(self) -> list[int]:
        now = now_utc()
        return [message_id for message_id, until in self.confirmations.items() if until < now]
