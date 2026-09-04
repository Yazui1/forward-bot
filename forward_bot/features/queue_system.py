from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import random
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import timedelta
from io import BytesIO
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from forward_bot.cache.transient import TransientMessage, TransientStore
from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.features.credits import loss_rate
from forward_bot.features.media import MediaService
from forward_bot.features.tombstone_media import removed_photo_media
from forward_bot.logging_utils import AggregateLogger, is_message_not_found_error, log_telegram_error
from forward_bot.utils import html_escape, now_utc, parse_dt


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliveryItem:
    message_id: int
    sender_id: int | None
    recipient_id: int
    priority: float
    content_type: str
    text: str | None
    media_file_id: str | None
    thumbnail_file_id: str | None
    media_kind: str | None
    mime_type: str | None
    sticker_set_name: str | None
    is_system: bool
    urgent: bool
    reply_to_message_id: int | None
    parse_mode: str | None
    remove_buttons: bool
    media_hash: str | None
    forward_from_chat_id: int | None
    forward_from_message_id: int | None
    system_html: bool = False
    reply_to_mod_note: bool = False
    delivery_bucket: str = "active"
    cancelled: bool = False
    started: bool = False


class DeliveryQueue:
    def __init__(self, config: Config, repo: Repository, store: TransientStore, media: MediaService, aggregate_logger: AggregateLogger | None = None):
        self.config = config
        self.repo = repo
        self.store = store
        self.media = media
        self.rate_per_second = float(config.get(
            "delivery.telegram_rate_limit_per_second", 25) or 25)
        self.per_recipient_rate_per_second = float(config.get(
            "delivery.per_recipient_rate_limit_per_second", 1) or 1)
        self.active_window = timedelta(hours=float(
            config.get("delivery.active_window_hours", 72) or 72))
        # Workers wait on network I/O, while wait_for_global_rate controls the
        # actual Telegram request rate. A single worker therefore limits the
        # queue to roughly one request latency instead of the configured rate.
        self.worker_count = max(
            1,
            int(config.get("delivery.worker_count", 32) or 32),
        )
        self._bot: Bot | None = None
        self._aggregate_logger = aggregate_logger
        self._queue: list[tuple[float, int, DeliveryItem]] = []
        self._event = asyncio.Event()
        self._seq = itertools.count()
        self._recipient_pending: dict[int,
                                      deque[DeliveryItem]] = defaultdict(deque)
        self._recipient_queued: set[int] = set()
        self._recipient_heap_item: dict[int, DeliveryItem] = {}
        self._recipient_locks: dict[int,
                                    asyncio.Lock] = defaultdict(asyncio.Lock)
        self._rate_lock = asyncio.Lock()
        self._recipient_last_send: dict[int, float] = defaultdict(float)
        self._recipient_pause_until: dict[int, float] = defaultdict(float)
        self._recipient_wake_tasks: dict[int, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []
        self._stopping = False
        self._last_send = 0.0
        self._pending_counts: dict[int, int] = defaultdict(int)
        self._completed_counts: dict[int, int] = defaultdict(int)
        self._recipient_hashes: set[tuple[int, str]] = set()
        self._inflight_items: dict[str, DeliveryItem] = {}
        self._bypass_tasks: set[asyncio.Task] = set()
        self._bypass_pending_items: dict[int, list[DeliveryItem]] = {}
        self._bypass_deferred_items: dict[int, list[DeliveryItem]] = {}
        self._queued_item_index: dict[tuple[int, int], DeliveryItem] = {}
        self._queued_item_location: dict[int, tuple[str, int | None]] = {}
        self._fair_bucket_finish: dict[str, float] = defaultdict(float)
        self._fair_floor = 0.0
        self._fair_weights = {
            "active": 4.0,
            "warm": 1.0,
            "inactive": 1.0,
        }

    def update_config(self, config: Config) -> None:
        self.config = config
        self.rate_per_second = float(config.get(
            "delivery.telegram_rate_limit_per_second", 25) or 25)
        self.per_recipient_rate_per_second = float(config.get(
            "delivery.per_recipient_rate_limit_per_second", 1) or 1)
        self.active_window = timedelta(hours=float(
            config.get("delivery.active_window_hours", 72) or 72))

    async def start(self, bot: Bot) -> None:
        self._bot = bot
        self._stopping = False
        self._workers = [asyncio.create_task(
            self._worker(), name=f"delivery-{i}") for i in range(self.worker_count)]

    async def stop(self) -> None:
        self._stopping = True
        self._event.set()
        for task in self._workers:
            task.cancel()
        for task in self._bypass_tasks:
            task.cancel()
        for task in self._recipient_wake_tasks.values():
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await asyncio.gather(*self._bypass_tasks, return_exceptions=True)
        self._bypass_tasks.clear()
        self._bypass_pending_items.clear()
        self._bypass_deferred_items.clear()
        self._queued_item_index.clear()
        self._queued_item_location.clear()
        await asyncio.gather(*self._recipient_wake_tasks.values(), return_exceptions=True)
        self._recipient_wake_tasks.clear()

    def enqueue_message(self, message: TransientMessage, recipients: list[User]) -> int:
        if not recipients:
            self.media.release(message.id)
            return 0
        self._pending_counts[message.id] += len(recipients)
        bypass_items: list[DeliveryItem] = []
        queued_items: list[DeliveryItem] = []
        for recipient in recipients:
            item = self._build_item(message, recipient)
            if recipient.is_mod_or_admin and self._bot:
                item.urgent = True
                item.priority = -1_000_000_000.0
                bypass_items.append(item)
            else:
                queued_items.append(item)
        if bypass_items and self._bot:
            self._bypass_pending_items[message.id] = bypass_items
            for item in bypass_items:
                self._index_queued_item(item, "bypass_pending", message.id)
            if queued_items:
                self._bypass_deferred_items[message.id] = queued_items
                for item in queued_items:
                    self._index_queued_item(item, "bypass_deferred", message.id)
            task = asyncio.create_task(
                self._send_bypass_then_enqueue(message.id, bypass_items, queued_items),
                name=f"delivery-bypass-{message.id}",
            )
            self._bypass_tasks.add(task)
            task.add_done_callback(self._bypass_tasks.discard)
        else:
            for item in queued_items:
                if item.urgent and self._bot:
                    self._enqueue_urgent(item)
                else:
                    self._enqueue_ordered(item)
        return len(recipients)

    def _build_item(self, message: TransientMessage, recipient: User) -> DeliveryItem:
        return DeliveryItem(
            message_id=message.id,
            sender_id=message.sender_id,
            recipient_id=recipient.telegram_id,
            priority=0.0,
            content_type=message.content_type,
            text=message.text,
            media_file_id=message.media_file_id,
            thumbnail_file_id=message.thumbnail_file_id,
            media_kind=message.media_kind,
            mime_type=message.mime_type,
            sticker_set_name=message.sticker_set_name,
            is_system=message.is_system,
            urgent=message.urgent or message.is_system,
            reply_to_message_id=message.reply_to_message_id,
            parse_mode=message.parse_mode,
            remove_buttons=message.remove_buttons,
            media_hash=message.media_hash,
            forward_from_chat_id=message.metadata.get(
                "forward_from_chat_id"),
            forward_from_message_id=message.metadata.get(
                "forward_from_message_id"),
            system_html=bool(message.metadata.get("system_html")),
            reply_to_mod_note=bool(message.metadata.get("reply_to_mod_note")),
            delivery_bucket=self._delivery_bucket(recipient),
        )

    async def _send_bypass_then_enqueue(
        self,
        message_id: int,
        bypass_items: list[DeliveryItem],
        queued_items: list[DeliveryItem],
    ) -> None:
        try:
            await asyncio.gather(*(self._send_bypass_item(item) for item in bypass_items))
        finally:
            for item in self._bypass_pending_items.pop(message_id, []) or []:
                self._unindex_queued_item(item)
            queued_items = self._bypass_deferred_items.pop(message_id, queued_items)
            if self._stopping:
                return
            for item in queued_items:
                self._unindex_queued_item(item)
                if item.urgent and self._bot:
                    self._enqueue_urgent(item)
                else:
                    self._enqueue_ordered(item)

    async def _send_bypass_item(self, item: DeliveryItem) -> None:
        if item.cancelled:
            return
        self._unindex_queued_item(item)
        key = f"bypass:{item.recipient_id}:{item.message_id}"
        self._inflight_items[key] = item
        try:
            while not self._stopping:
                item.started = True
                status = await self._send_with_backoff(item)
                if status == "requeued":
                    item.started = False
                    self._enqueue_urgent(item)
                    return
                self._finish_item(item, status, ordered=False)
                return
            self._finish_item(item, "ineligible", ordered=False)
        finally:
            self._inflight_items.pop(key, None)

    def on_user_activity(self, user_id: int) -> None:
        user = self.repo.get_user(user_id)
        if not user:
            return
        self.store.inactive_notified.discard(user_id)
        for item in self._recipient_pending.get(user_id, ()):
            self._refresh_item_for_user(item, user)
        for items in self._bypass_deferred_items.values():
            for item in items:
                if item.recipient_id == user_id:
                    self._refresh_item_for_user(item, user)
        for items in self._bypass_pending_items.values():
            for item in items:
                if item.recipient_id == user_id:
                    self._refresh_item_for_user(item, user)
        queued = self._recipient_heap_item.get(user_id)
        if queued and not queued.cancelled and not queued.started:
            self._refresh_item_for_user(queued, user)
            queued.priority = self._queue_priority(queued)
            self._queue = [
                (queued.priority if heap_item is queued else priority, seq, heap_item)
                for priority, seq, heap_item in self._queue
            ]
            heapq.heapify(self._queue)
            self._event.set()
        elif user_id not in self._recipient_queued and self._recipient_pending.get(user_id):
            self._queue_one_for_recipient(user_id)

    def _refresh_item_for_user(self, item: DeliveryItem, user: User) -> None:
        item.delivery_bucket = self._delivery_bucket(user)

    def promote_deleted_message(self, message_id: int) -> None:
        urgent_priority = -1_000_000_000.0
        touched = False
        for item in self._recipient_heap_item.values():
            if item.message_id == message_id and not item.started and not item.cancelled:
                item.priority = urgent_priority
                item.urgent = True
                touched = True
        if touched:
            self._queue = [
                (urgent_priority if item.message_id ==
                 message_id and not item.started and not item.cancelled else priority, seq, item)
                for priority, seq, item in self._queue
            ]
            heapq.heapify(self._queue)
            self._event.set()

    def _delivery_bucket(self, recipient: User) -> str:
        last = parse_dt(recipient.last_activity)
        if not last:
            return "active"
        age = now_utc() - last
        if age <= self.active_window:
            return "active"
        return "inactive" if self._past_inactivity_period(recipient) else "warm"

    def _enqueue_ordered(self, item: DeliveryItem) -> None:
        pending = self._recipient_pending[item.recipient_id]
        pending.append(item)
        self._index_queued_item(item, "pending", None)
        if item.recipient_id not in self._recipient_queued:
            self._queue_one_for_recipient(item.recipient_id)

    def _enqueue_urgent(self, item: DeliveryItem) -> None:
        pending = self._recipient_pending[item.recipient_id]
        queued = self._recipient_heap_item.get(item.recipient_id)
        if queued and not queued.urgent and not queued.cancelled and not queued.started:
            queued.cancelled = True
            self._unindex_queued_item(queued)
            self._recipient_queued.discard(item.recipient_id)
            restored = replace(queued, cancelled=False)
            pending.appendleft(restored)
            self._index_queued_item(restored, "pending", None)
        pending.appendleft(item)
        self._index_queued_item(item, "pending", None)
        if item.recipient_id not in self._recipient_queued:
            self._queue_one_for_recipient(item.recipient_id)

    def _queue_one_for_recipient(self, recipient_id: int) -> None:
        pending = self._recipient_pending.get(recipient_id)
        if not pending:
            self._recipient_queued.discard(recipient_id)
            return
        wait = self._recipient_wait_seconds(recipient_id)
        if wait > 0:
            self._recipient_queued.discard(recipient_id)
            self._schedule_recipient_wake(recipient_id, wait)
            return
        item = pending.popleft()
        self._unindex_queued_item(item)
        item.priority = self._queue_priority(item)
        self._recipient_queued.add(recipient_id)
        self._recipient_heap_item[recipient_id] = item
        self._index_queued_item(item, "heap", None)
        heapq.heappush(self._queue, (item.priority, next(self._seq), item))
        self._event.set()

    def _queue_priority(self, item: DeliveryItem) -> float:
        if item.urgent:
            return -1_000_000_000.0
        bucket = item.delivery_bucket
        weight = max(0.1, self._fair_weights.get(bucket, 1.0))
        start = max(self._fair_bucket_finish[bucket], self._fair_floor)
        finish = start + (1.0 / weight)
        self._fair_bucket_finish[bucket] = finish
        self._fair_floor = max(self._fair_floor, finish - 1.0)
        return finish

    def _index_queued_item(self, item: DeliveryItem, location: str, owner_message_id: int | None) -> None:
        self._queued_item_index[(item.message_id, item.recipient_id)] = item
        self._queued_item_location[id(item)] = (location, owner_message_id)

    def _unindex_queued_item(self, item: DeliveryItem) -> None:
        key = (item.message_id, item.recipient_id)
        if self._queued_item_index.get(key) is item:
            self._queued_item_index.pop(key, None)
        self._queued_item_location.pop(id(item), None)

    async def _wake_recipient_after(self, recipient_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            self._recipient_wake_tasks.pop(recipient_id, None)
            if recipient_id not in self._recipient_queued and self._recipient_pending.get(recipient_id):
                self._queue_one_for_recipient(recipient_id)
        except asyncio.CancelledError:
            raise

    async def _worker(self) -> None:
        while not self._stopping:
            if not self._queue:
                self._event.clear()
                await self._event.wait()
                continue
            _, _, item = heapq.heappop(self._queue)
            if item.cancelled:
                continue
            item.started = True
            worker_name = asyncio.current_task().get_name() if asyncio.current_task() else "delivery"
            self._inflight_items[worker_name] = item
            try:
                status = await self._send_with_backoff(item)
            finally:
                self._inflight_items.pop(worker_name, None)
            self._finish_item(item, status)

    def snapshot(self) -> dict[str, Any]:
        loop_time = asyncio.get_running_loop().time()
        heap_items = [item for _, _, item in self._queue]
        live_heap_items = [item for item in heap_items if not item.cancelled]
        pending_items = [item for pending in self._recipient_pending.values() for item in pending if not item.cancelled]
        bypass_pending_items = [item for items in self._bypass_pending_items.values() for item in items if not item.cancelled]
        deferred_items = [item for items in self._bypass_deferred_items.values() for item in items if not item.cancelled]
        inflight_items = list(self._inflight_items.values())
        all_open_items = live_heap_items + pending_items + bypass_pending_items + deferred_items + inflight_items
        paused = {
            recipient_id: max(0.0, until - loop_time)
            for recipient_id, until in self._recipient_pause_until.items()
            if until > loop_time
        }
        message_pending = {
            message_id: {
                "pending": int(self._pending_counts.get(message_id, 0)),
                "completed": int(self._completed_counts.get(message_id, 0)),
                "open": 0,
                "recipients": set(),
            }
            for message_id in self._pending_counts
        }
        for item in all_open_items:
            entry = message_pending.setdefault(
                item.message_id,
                {"pending": 0, "completed": 0, "open": 0, "recipients": set()},
            )
            entry["open"] += 1
            entry["recipients"].add(item.recipient_id)
        top_messages = []
        now = now_utc()
        for message_id, data in message_pending.items():
            msg = self.store.get_message(message_id)
            age_seconds = int((now - msg.created_at).total_seconds()) if msg else None
            top_messages.append({
                "message_id": message_id,
                "sender_id": msg.sender_id if msg else None,
                "content_type": msg.content_type if msg else None,
                "age_seconds": age_seconds,
                "pending": data["pending"],
                "completed": data["completed"],
                "open": data["open"],
                "recipient_count": len(data["recipients"]),
                "deleted": bool(msg.deleted) if msg else None,
                "urgent": bool(msg.urgent) if msg else None,
                "system": bool(msg.is_system) if msg else None,
            })
        top_messages.sort(key=lambda item: (item["open"], item["pending"], item["age_seconds"] or 0), reverse=True)
        pending_by_recipient = Counter(item.recipient_id for item in all_open_items)
        top_recipients = []
        for recipient_id, count in pending_by_recipient.most_common(15):
            user = self.repo.get_user(recipient_id)
            pause_seconds = paused.get(recipient_id, 0.0)
            last_activity = parse_dt(user.last_activity) if user else None
            top_recipients.append({
                "recipient_id": recipient_id,
                "pending": count,
                "paused_seconds": int(pause_seconds),
                "queued": recipient_id in self._recipient_queued,
                "heap_item": recipient_id in self._recipient_heap_item,
                "wake_task": recipient_id in self._recipient_wake_tasks and not self._recipient_wake_tasks[recipient_id].done(),
                "started": bool(user.has_started) if user else None,
                "banned": bool(user.is_banned) if user else None,
                "mod": bool(user.is_mod_or_admin) if user else None,
                "last_activity_age_seconds": int((now - last_activity).total_seconds()) if last_activity else None,
            })
        content_types = Counter(item.content_type for item in all_open_items)
        statuses = Counter()
        for stats in self.store.delivery_stats.values():
            statuses.update(stats)
        ages = [item["age_seconds"] for item in top_messages if item["age_seconds"] is not None]
        recipient_backlogs = list(pending_by_recipient.values())
        min_global_gap = 1.0 / max(1.0, self.rate_per_second)
        global_wait = max(0.0, self._last_send + min_global_gap - loop_time)
        workers = [
            {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
                "inflight_message_id": self._inflight_items.get(task.get_name()).message_id if self._inflight_items.get(task.get_name()) else None,
                "inflight_recipient_id": self._inflight_items.get(task.get_name()).recipient_id if self._inflight_items.get(task.get_name()) else None,
            }
            for task in self._workers
        ]
        return {
            "running": bool(self._bot and not self._stopping),
            "stopping": self._stopping,
            "worker_count_config": self.worker_count,
            "workers": workers,
            "rate_per_second": self.rate_per_second,
            "per_recipient_rate_per_second": self.per_recipient_rate_per_second,
            "global_wait_seconds": int(global_wait),
            "active_window_seconds": int(self.active_window.total_seconds()),
            "heap_size": len(self._queue),
            "heap_live": len(live_heap_items),
            "heap_cancelled": len(heap_items) - len(live_heap_items),
            "recipient_pending_items": len(pending_items),
            "recipient_pending_recipients": sum(1 for pending in self._recipient_pending.values() if pending),
            "bypass_tasks": sum(1 for task in self._bypass_tasks if not task.done()),
            "bypass_pending_items": len(bypass_pending_items),
            "bypass_deferred_items": len(deferred_items),
            "recipient_queued": len(self._recipient_queued),
            "recipient_heap_items": len(self._recipient_heap_item),
            "inflight": len(inflight_items),
            "open_items": len(all_open_items),
            "pending_messages": len(self._pending_counts),
            "pending_fanout_total": sum(self._pending_counts.values()),
            "completed_fanout_total": sum(self._completed_counts.values()),
            "paused_recipients": len(paused),
            "max_pause_seconds": int(max(paused.values(), default=0.0)),
            "oldest_open_message_age_seconds": max(ages, default=0),
            "max_recipient_backlog": max(recipient_backlogs, default=0),
            "wake_tasks": sum(1 for task in self._recipient_wake_tasks.values() if not task.done()),
            "content_types": dict(content_types),
            "delivery_statuses": dict(statuses),
            "top_messages": top_messages[:15],
            "top_recipients": top_recipients,
        }

    def _finish_item(self, item: DeliveryItem, status: str, *, ordered: bool = True) -> None:
        if status == "requeued":
            if ordered:
                self._recipient_queued.discard(item.recipient_id)
                if self._recipient_heap_item.get(item.recipient_id) is item:
                    self._recipient_heap_item.pop(item.recipient_id, None)
                self._unindex_queued_item(item)
                item.started = False
                self._recipient_pending[item.recipient_id].appendleft(item)
                self._index_queued_item(item, "pending", None)
                self._queue_one_for_recipient(item.recipient_id)
            return
        if self._aggregate_logger:
            self._aggregate_logger.increment(f"delivery.{status}")
        self.store.record_delivery_status(item.message_id, status)
        self._completed_counts[item.message_id] += 1
        if ordered:
            self._recipient_queued.discard(item.recipient_id)
            if self._recipient_heap_item.get(item.recipient_id) is item:
                self._recipient_heap_item.pop(item.recipient_id, None)
            self._unindex_queued_item(item)
            self._queue_one_for_recipient(item.recipient_id)
        if self._pending_counts[item.message_id] <= self._completed_counts[item.message_id]:
            self.media.release(item.message_id)
            self._pending_counts.pop(item.message_id, None)
            self._completed_counts.pop(item.message_id, None)

    async def _send_with_backoff(self, item: DeliveryItem) -> str:
        for attempt in range(3):
            try:
                return await self._send(item)
            except RetryAfter as exc:
                retry_after = float(getattr(exc, "retry_after", 1) or 1)
                log_telegram_error(LOGGER, "delivery.retry_after", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id, retry_after=retry_after)
                self._pause_recipient_for_retry_after(
                    item.recipient_id, retry_after)
                return "requeued"
            except BadRequest as exc:
                if _is_reply_rejection(exc):
                    LOGGER.info(
                        "telegram delivery.reply_rejected failed: %s fields=%s",
                        exc,
                        {"message_id": item.message_id, "recipient_id": item.recipient_id},
                    )
                    if self._aggregate_logger:
                        self._aggregate_logger.increment("telegram.delivery.reply_rejected.BadRequest")
                    return "reply_rejected"
                log_telegram_error(LOGGER, "delivery.bad_request", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id)
                msg = str(exc).lower()
                if "chat not found" in msg or "bot was blocked" in msg:
                    self.repo.mark_left(item.recipient_id)
                    return "chat_not_found_left"
                return "bad_request"
            except (TimedOut, NetworkError) as exc:
                log_telegram_error(LOGGER, "delivery.network_retry", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id, attempt=attempt + 1)
                await asyncio.sleep(min(10.0, 1.5 * (attempt + 1)))
            except Forbidden as exc:
                log_telegram_error(LOGGER, "delivery.forbidden", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id)
                self.repo.mark_left(item.recipient_id)
                return "forbidden_left"
            except ChatMigrated as exc:
                log_telegram_error(LOGGER, "delivery.chat_migrated", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id)
                self.repo.mark_left(item.recipient_id)
                return "chat_not_found_left"
            except TelegramError as exc:
                log_telegram_error(LOGGER, "delivery.telegram_error", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id)
                return "telegram_error"
            except Exception:
                return "unexpected_error"
        return "telegram_error"

    async def _send(self, item: DeliveryItem) -> str:
        if not self._bot:
            return "telegram_error"
        async with self._recipient_locks[item.recipient_id]:
            return await self._send_locked(item)

    async def _send_locked(self, item: DeliveryItem) -> str:
        recipient = self.repo.get_user(item.recipient_id)
        if not recipient or recipient.is_banned or not recipient.has_started:
            return "ineligible"
        current_message = self.store.get_message(item.message_id)
        if not current_message:
            return "ineligible"
        reply_to = None
        if item.reply_to_message_id:
            reply_to = self._reply_to_for_recipient(
                item, recipient.telegram_id)
            if not reply_to:
                reply_to = await self._ensure_reply_dependency_locked(item, recipient)
            if not reply_to:
                return "reply_missing"
        if item.media_hash and recipient.filter_duplicates and not recipient.is_mod_or_admin:
            key = (recipient.telegram_id, item.media_hash)
            if key in self._recipient_hashes:
                return "duplicate_filtered"
            self._recipient_hashes.add(key)

        if self._inactive_drop(recipient, item):
            return "inactive_drop"

        wait = self._recipient_wait_seconds(item.recipient_id)
        if wait > 0:
            self._schedule_recipient_wake(item.recipient_id, wait)
            return "requeued"
        self._mark_recipient_send(item.recipient_id)
        await self.wait_for_global_rate()
        if current_message.removed_for_mods and recipient.is_mod_or_admin:
            try:
                return await self._send_deleted_tombstone(item, reply_to)
            except BadRequest as exc:
                if reply_to and _is_reply_rejection(exc):
                    log_telegram_error(LOGGER, "delivery.reply_rejected", exc, aggregate=self._aggregate_logger,
                                       message_id=item.message_id, recipient_id=item.recipient_id, reply_to=reply_to)
                    return "reply_rejected"
                raise
        if current_message.deleted:
            try:
                return await self._send_deleted_tombstone(item, reply_to)
            except BadRequest as exc:
                if reply_to and _is_reply_rejection(exc):
                    log_telegram_error(LOGGER, "delivery.reply_rejected", exc, aggregate=self._aggregate_logger,
                                       message_id=item.message_id, recipient_id=item.recipient_id, reply_to=reply_to)
                    return "reply_rejected"
                raise

        blurred = await self._should_blur(recipient, item)
        markup = self._reply_markup(item, recipient)
        try:
            sent = await self._send_content(item, reply_to, markup, blurred)
        except BadRequest as exc:
            if reply_to and _is_reply_rejection(exc):
                log_telegram_error(LOGGER, "delivery.reply_rejected", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id, reply_to=reply_to)
                return "reply_rejected"
            else:
                raise
        if not sent:
            return "bad_request"
        delivery = self.store.add_delivery(
            item.message_id, recipient.telegram_id, sent.message_id, blurred=blurred)
        if self._message_deleted(item.message_id):
            await self._tombstone_existing_delivery(delivery, reply_to, item.content_type)
            return "deleted_tombstone"
        return "sent_blurred" if blurred else "sent"

    async def ensure_delivery(self, message_id: int, recipient_id: int) -> int | None:
        reply_to = self.store.delivery_reply_for_recipient(message_id, recipient_id)
        if reply_to:
            return reply_to
        if not self._bot:
            return None
        status = None
        async with self._recipient_locks[recipient_id]:
            reply_to = self.store.delivery_reply_for_recipient(message_id, recipient_id)
            if reply_to:
                return reply_to
            item, ordered = self._take_queued_delivery(message_id, recipient_id)
            if not item:
                status = "missing"
                item = None
            if not item:
                pass
            else:
                item.started = True
                key = f"dependency:{recipient_id}:{message_id}"
                self._inflight_items[key] = item
                try:
                    status = await self._send_locked(item)
                finally:
                    self._inflight_items.pop(key, None)
                self._finish_item(item, status, ordered=ordered)
                if status in {"sent", "sent_blurred", "deleted_tombstone"}:
                    return self.store.delivery_reply_for_recipient(message_id, recipient_id)
        return await self._wait_for_delivery(message_id, recipient_id) if status in {"missing", "requeued"} else None

    async def _ensure_reply_dependency_locked(self, item: DeliveryItem, recipient: User) -> int | None:
        if item.reply_to_mod_note or not item.reply_to_message_id:
            return None
        reply_to = self._reply_to_for_recipient(item, recipient.telegram_id)
        if reply_to:
            return reply_to
        parent, ordered = self._take_queued_delivery(item.reply_to_message_id, recipient.telegram_id)
        if not parent:
            return None
        parent.started = True
        key = f"dependency:{recipient.telegram_id}:{parent.message_id}"
        self._inflight_items[key] = parent
        try:
            status = await self._send_locked(parent)
        finally:
            self._inflight_items.pop(key, None)
        self._finish_item(parent, status, ordered=ordered)
        if status in {"sent", "sent_blurred", "deleted_tombstone"}:
            return self._reply_to_for_recipient(item, recipient.telegram_id)
        return None

    async def _wait_for_delivery(self, message_id: int, recipient_id: int, *, timeout: float = 5.0) -> int | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            reply_to = self.store.delivery_reply_for_recipient(message_id, recipient_id)
            if reply_to:
                return reply_to
            if loop.time() >= deadline or self._stopping:
                return None
            await asyncio.sleep(0.05)

    def _take_queued_delivery(self, message_id: int, recipient_id: int) -> tuple[DeliveryItem | None, bool]:
        item = self._queued_item_index.get((message_id, recipient_id))
        if not item or item.started or item.cancelled:
            return None, False
        location, owner_message_id = self._queued_item_location.get(id(item), ("", None))
        if location == "heap" and self._recipient_heap_item.get(recipient_id) is item:
            item.cancelled = True
            self._recipient_queued.discard(recipient_id)
            self._recipient_heap_item.pop(recipient_id, None)
            self._unindex_queued_item(item)
            return replace(item, cancelled=False, started=False), True
        if location == "pending":
            pending = self._recipient_pending.get(recipient_id)
            if pending and item in pending:
                pending.remove(item)
                self._unindex_queued_item(item)
                return item, False
        if location == "bypass_deferred" and owner_message_id is not None:
            items = self._bypass_deferred_items.get(owner_message_id)
            if items and item in items:
                items.remove(item)
                self._unindex_queued_item(item)
                return item, False
        if location == "bypass_pending" and owner_message_id is not None:
            items = self._bypass_pending_items.get(owner_message_id)
            if items and item in items:
                item.cancelled = True
                items.remove(item)
                self._unindex_queued_item(item)
                return replace(item, cancelled=False, started=False), False
        return None, False

    async def _send_deleted_tombstone(self, item: DeliveryItem, reply_to: int | None) -> str:
        assert self._bot is not None
        sent = await self._bot.send_message(
            chat_id=item.recipient_id,
            text="<i>Message removed.</i>",
            parse_mode="HTML",
            reply_to_message_id=reply_to,
        )
        delivery = self.store.add_delivery(
            item.message_id, item.recipient_id, sent.message_id)
        self.store.mark_delivery_deleted(
            delivery.id, tombstone_message_id=sent.message_id, kind="queued_tombstone")
        return "deleted_tombstone"

    async def _tombstone_existing_delivery(self, delivery, reply_to: int | None, content_type: str) -> None:
        assert self._bot is not None
        if content_type == "text" or content_type in {"photo", "video", "animation", "document"}:
            try:
                await self.wait_for_global_rate()
                if content_type == "text":
                    await self._bot.edit_message_text(
                        chat_id=delivery.recipient_id,
                        message_id=delivery.telegram_message_id,
                        text="<i>Message removed.</i>",
                        parse_mode="HTML",
                    )
                else:
                    await self._bot.edit_message_media(
                        chat_id=delivery.recipient_id,
                        message_id=delivery.telegram_message_id,
                        media=removed_photo_media("<i>Message removed.</i>"),
                    )
                self.store.mark_delivery_deleted(delivery.id, tombstone_message_id=delivery.telegram_message_id,
                                                 kind="media_edited" if content_type != "text" else "edited")
                return
            except TelegramError as exc:
                log_telegram_error(LOGGER, "delivery.tombstone_edit", exc, aggregate=self._aggregate_logger,
                                   recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id, content_type=content_type)
                if is_message_not_found_error(exc):
                    self.store.mark_delivery_deleted(
                        delivery.id, tombstone_message_id=None, kind="already_missing")
                    return
        try:
            await self.wait_for_global_rate()
            await self._bot.delete_message(delivery.recipient_id, delivery.telegram_message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "delivery.tombstone_delete", exc, aggregate=self._aggregate_logger,
                               recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id)
            kind = "already_missing" if is_message_not_found_error(exc) else "delete_failed"
        else:
            kind = "deleted"
        self.store.mark_delivery_deleted(
            delivery.id, tombstone_message_id=None, kind=kind)

    def _message_deleted(self, message_id: int) -> bool:
        message = self.store.get_message(message_id)
        return bool(message and message.deleted)

    async def wait_for_global_rate(self) -> None:
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            min_gap = 1.0 / max(1.0, self.rate_per_second)
            now = loop.time()
            wait = self._last_send + min_gap - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = loop.time()

    def _recipient_wait_seconds(self, recipient_id: int) -> float:
        loop = asyncio.get_running_loop()
        pause_wait = self._recipient_pause_until[recipient_id] - loop.time()
        min_gap = 1.0 / max(0.1, self.per_recipient_rate_per_second)
        now = loop.time()
        rate_wait = self._recipient_last_send[recipient_id] + min_gap - now
        return max(0.0, pause_wait, rate_wait)

    def _mark_recipient_send(self, recipient_id: int) -> None:
        self._recipient_last_send[recipient_id] = asyncio.get_running_loop().time()

    def _schedule_recipient_wake(self, recipient_id: int, delay: float) -> None:
        task = self._recipient_wake_tasks.get(recipient_id)
        if task is None or task.done():
            self._recipient_wake_tasks[recipient_id] = asyncio.create_task(
                self._wake_recipient_after(recipient_id, delay),
                name=f"delivery-wake-{recipient_id}",
            )

    def _pause_recipient_for_retry_after(self, recipient_id: int, retry_after: float) -> None:
        loop = asyncio.get_running_loop()
        pause_until = loop.time() + max(1.0, retry_after)
        self._recipient_pause_until[recipient_id] = max(
            self._recipient_pause_until[recipient_id], pause_until)

    def _inactive_drop(self, recipient: User, item: DeliveryItem) -> bool:
        if not self._should_inactive_drop(recipient, item):
            return False
        self._notify_inactive(recipient)
        return True

    def _should_inactive_drop(self, recipient: User, item: DeliveryItem) -> bool:
        if item.is_system or recipient.is_mod_or_admin:
            return False
        if not self._past_inactivity_period(recipient):
            self.store.inactive_notified.discard(recipient.telegram_id)
            return False
        if item.content_type == "text":
            return True
        chance = float(self.config.get(
            "inactivity.non_system_receive_chance", 0.05) or 0.05)
        return random.random() > chance

    def _past_inactivity_period(self, recipient: User) -> bool:
        period_days = float(self.config.get("inactivity.period_days", 4) or 4)
        last = parse_dt(recipient.last_activity)
        if not last or now_utc() - last <= timedelta(days=period_days):
            return False
        return True

    def _notify_inactive(self, recipient: User) -> None:
        if recipient.telegram_id not in self.store.inactive_notified and self._bot:
            self.store.inactive_notified.add(recipient.telegram_id)
            asyncio.create_task(self._bot.send_message(
                recipient.telegram_id, "You are currently inactive, so most non-system messages are not being delivered. Send meaningful messages or interact normally to become active again. Abuse such as dotposting or other low-effort activity padding is not allowed."))

    async def _should_blur(self, recipient: User, item: DeliveryItem) -> bool:
        if item.is_system or recipient.is_mod_or_admin or item.content_type == "text":
            return False
        return random.random() < loss_rate(self.config, recipient.credits)

    def _reply_markup(self, item: DeliveryItem, recipient: User) -> InlineKeyboardMarkup | None:
        if item.is_system or not item.remove_buttons or not recipient.vote_buttons_enabled:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton("Vote remove", callback_data=f"rm:{item.message_id}")]])

    async def _send_content(self, item: DeliveryItem, reply_to: int | None, markup: InlineKeyboardMarkup | None, blurred: bool):
        assert self._bot is not None
        kwargs = {"chat_id": item.recipient_id,
                  "reply_to_message_id": reply_to, "reply_markup": markup}
        if item.is_system:
            kwargs["parse_mode"] = "HTML"
        elif item.parse_mode:
            kwargs["parse_mode"] = item.parse_mode
        if item.forward_from_chat_id and item.forward_from_message_id and not item.is_system and not blurred and not reply_to and not markup:
            try:
                return await self._bot.forward_message(
                    chat_id=item.recipient_id,
                    from_chat_id=item.forward_from_chat_id,
                    message_id=item.forward_from_message_id,
                )
            except BadRequest as exc:
                if _is_forward_rejection(exc):
                    LOGGER.info(
                        "telegram delivery.forward_fallback skipped: %s fields=%s",
                        exc,
                        {"message_id": item.message_id, "recipient_id": item.recipient_id},
                    )
                    if self._aggregate_logger:
                        self._aggregate_logger.increment("telegram.delivery.forward_fallback.BadRequest")
                else:
                    log_telegram_error(LOGGER, "delivery.forward_fallback", exc, aggregate=self._aggregate_logger,
                                       message_id=item.message_id, recipient_id=item.recipient_id)
                pass
        if blurred:
            notice = "Uh oh 😭 The message was blurred due to your loss rate, earn credits to reduce your loss rate, see /help, /info and /creditstats for more info."
            caption = f"{item.text}\n\n{notice}" if item.text else notice
            cached_file_id = self.media.blurred_file_id(item.message_id)
            if cached_file_id:
                return await self._bot.send_photo(photo=cached_file_id, caption=caption, **kwargs)
            async with self.media.blur_upload_lock(item.message_id):
                cached_file_id = self.media.blurred_file_id(item.message_id)
                if cached_file_id:
                    return await self._bot.send_photo(photo=cached_file_id, caption=caption, **kwargs)
                preview = await self.media.blurred_preview(item.message_id)
                if preview:
                    sent = await self._bot.send_photo(
                        photo=InputFile(BytesIO(preview),
                                        filename="blurred.jpg"),
                        caption=caption,
                        **kwargs,
                    )
                    if sent.photo:
                        self.media.set_blurred_file_id(
                            item.message_id, sent.photo[-1].file_id)
                    return sent
            text = f"{item.text}\n\n{notice}" if item.text else notice
            return await self._bot.send_message(text=text, **kwargs)
        if item.content_type == "text":
            return await self._bot.send_message(text=_render_text(item.text, item.is_system, fallback="Message unavailable.", trusted_html=item.system_html), **kwargs)
        if item.content_type == "photo":
            return await self._bot.send_photo(photo=item.media_file_id, caption=_render_text(item.text, item.is_system), **kwargs)
        if item.content_type == "video":
            return await self._bot.send_video(video=item.media_file_id, caption=_render_text(item.text, item.is_system), **kwargs)
        if item.content_type == "animation":
            return await self._bot.send_animation(animation=item.media_file_id, caption=_render_text(item.text, item.is_system), **kwargs)
        if item.content_type == "sticker":
            return await self._bot.send_sticker(sticker=item.media_file_id, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        if item.content_type == "document":
            return await self._bot.send_document(document=item.media_file_id, caption=_render_text(item.text, item.is_system), **kwargs)
        if item.content_type == "video_note":
            return await self._bot.send_video_note(video_note=item.media_file_id, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        return await self._bot.send_message(text=_render_text(item.text, item.is_system, fallback="[unsupported message]", trusted_html=item.system_html), **kwargs)

    def _reply_to_for_recipient(self, item: DeliveryItem, recipient_id: int) -> int | None:
        if not item.reply_to_message_id:
            return None
        if item.reply_to_mod_note:
            return self.store.mod_note_for_recipient(item.reply_to_message_id, recipient_id)
        prior = self.store.delivery_for_recipient(
            item.reply_to_message_id, recipient_id)
        if prior:
            return prior.telegram_message_id
        deleted_prior = self.store.delivery_reply_for_recipient(item.reply_to_message_id, recipient_id)
        if deleted_prior:
            return deleted_prior
        original = self.store.get_message(item.reply_to_message_id)
        if original and original.sender_id == recipient_id and original.source_chat_id == recipient_id:
            return original.source_message_id
        return None


def _is_reply_rejection(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "reply" in text and ("not found" in text or "message to be replied" in text or "invalid" in text)


def _is_forward_rejection(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "forward" in text and "not found" in text


def _render_text(text: str | None, is_system: bool, *, fallback: str | None = None, trusted_html: bool = False) -> str | None:
    value = text or fallback
    if value is None:
        return None
    if is_system:
        if trusted_html:
            return f"<i>{value}</i>"
        return f"<i>{html_escape(value)}</i>"
    return value
