from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import random
from collections import defaultdict, deque
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
from forward_bot.logging_utils import AggregateLogger, log_telegram_error
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
        self.worker_count = int(config.get("delivery.worker_count", 1) or 1)
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
        self._workers: list[asyncio.Task] = []
        self._stopping = False
        self._last_send = 0.0
        self._pending_counts: dict[int, int] = defaultdict(int)
        self._completed_counts: dict[int, int] = defaultdict(int)
        self._recipient_hashes: set[tuple[int, str]] = set()

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
        await asyncio.gather(*self._workers, return_exceptions=True)

    def enqueue_message(self, message: TransientMessage, recipients: list[User]) -> int:
        if not recipients:
            self.media.release(message.id)
            return 0
        self._pending_counts[message.id] += len(recipients)
        for recipient in recipients:
            item = DeliveryItem(
                message_id=message.id,
                sender_id=message.sender_id,
                recipient_id=recipient.telegram_id,
                priority=self._priority(
                    recipient, urgent=message.urgent or message.is_system),
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
            )
            if item.urgent and self._bot:
                self._enqueue_urgent(item)
            else:
                self._enqueue_ordered(item)
        return len(recipients)

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

    def _priority(self, recipient: User, *, urgent: bool) -> float:
        last = parse_dt(recipient.last_activity)
        if not last:
            base = 1_000_000.0
            return base - 0.5 if urgent else base
        age = now_utc() - last
        if age <= self.active_window:
            base = age.total_seconds()
        else:
            base = 500_000.0 + age.total_seconds()
        return base - 0.5 if urgent else base

    def _enqueue_ordered(self, item: DeliveryItem) -> None:
        pending = self._recipient_pending[item.recipient_id]
        pending.append(item)
        if item.recipient_id not in self._recipient_queued:
            self._queue_one_for_recipient(item.recipient_id)

    def _enqueue_urgent(self, item: DeliveryItem) -> None:
        pending = self._recipient_pending[item.recipient_id]
        queued = self._recipient_heap_item.get(item.recipient_id)
        if queued and not queued.urgent and not queued.cancelled and not queued.started:
            queued.cancelled = True
            self._recipient_queued.discard(item.recipient_id)
            pending.appendleft(replace(queued, cancelled=False))
        pending.appendleft(item)
        if item.recipient_id not in self._recipient_queued:
            self._queue_one_for_recipient(item.recipient_id)

    def _queue_one_for_recipient(self, recipient_id: int) -> None:
        pending = self._recipient_pending.get(recipient_id)
        if not pending:
            self._recipient_queued.discard(recipient_id)
            return
        item = pending.popleft()
        self._recipient_queued.add(recipient_id)
        self._recipient_heap_item[recipient_id] = item
        heapq.heappush(self._queue, (item.priority, next(self._seq), item))
        self._event.set()

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
            status = await self._send_with_backoff(item)
            self._finish_item(item, status)

    async def _send_urgent(self, item: DeliveryItem) -> None:
        status = await self._send_with_backoff(item)
        self._finish_item(item, status, ordered=False)

    def _finish_item(self, item: DeliveryItem, status: str, *, ordered: bool = True) -> None:
        if self._aggregate_logger:
            self._aggregate_logger.increment(f"delivery.{status}")
        self.store.record_delivery_status(item.message_id, status)
        self._completed_counts[item.message_id] += 1
        if ordered:
            self._recipient_queued.discard(item.recipient_id)
            if self._recipient_heap_item.get(item.recipient_id) is item:
                self._recipient_heap_item.pop(item.recipient_id, None)
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
                await self._pause_recipient_for_retry_after(item.recipient_id, retry_after)
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
            except BadRequest as exc:
                log_telegram_error(LOGGER, "delivery.bad_request", exc, aggregate=self._aggregate_logger,
                                   message_id=item.message_id, recipient_id=item.recipient_id)
                msg = str(exc).lower()
                if "chat not found" in msg or "bot was blocked" in msg:
                    self.repo.mark_left(item.recipient_id)
                    return "chat_not_found_left"
                return "bad_request"
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
            await self._respect_recipient_rate(item.recipient_id)
            await self._respect_global_rate()
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
                    return "reply_missing"
            if current_message.deleted:
                try:
                    return await self._send_deleted_tombstone(item, reply_to)
                except BadRequest as exc:
                    if reply_to and _is_reply_rejection(exc):
                        log_telegram_error(LOGGER, "delivery.reply_rejected", exc, aggregate=self._aggregate_logger,
                                           message_id=item.message_id, recipient_id=item.recipient_id, reply_to=reply_to)
                        return "reply_rejected"
                    raise

            if item.media_hash and recipient.filter_duplicates and not recipient.is_mod_or_admin:
                key = (recipient.telegram_id, item.media_hash)
                if key in self._recipient_hashes:
                    return "duplicate_filtered"
                self._recipient_hashes.add(key)

            if self._inactive_drop(recipient, item):
                return "inactive_drop"

            blurred = await self._should_blur(recipient, item)
            markup = self._reply_markup(item, recipient)
            try:
                sent = await self._send_content(item, reply_to, markup, blurred)
            except BadRequest as exc:
                if reply_to and _is_reply_rejection(exc):
                    log_telegram_error(LOGGER, "delivery.reply_rejected", exc, aggregate=self._aggregate_logger,
                                       message_id=item.message_id, recipient_id=item.recipient_id, reply_to=reply_to)
                    return "reply_rejected"
                raise
            if not sent:
                return "bad_request"
            delivery = self.store.add_delivery(
                item.message_id, recipient.telegram_id, sent.message_id, blurred=blurred)
            if self._message_deleted(item.message_id):
                await self._tombstone_existing_delivery(delivery, reply_to, item.content_type)
                return "deleted_tombstone"
            return "sent_blurred" if blurred else "sent"

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
        try:
            if content_type == "text":
                await self._bot.edit_message_text(
                    chat_id=delivery.recipient_id,
                    message_id=delivery.telegram_message_id,
                    text="<i>Message removed.</i>",
                    parse_mode="HTML",
                )
            elif content_type in {"photo", "video", "animation", "document"}:
                await self._bot.edit_message_media(
                    chat_id=delivery.recipient_id,
                    message_id=delivery.telegram_message_id,
                    media=removed_photo_media("<i>Message removed.</i>"),
                )
            else:
                raise TelegramError(
                    "content type cannot be tombstoned in-place")
            self.store.mark_delivery_deleted(delivery.id, tombstone_message_id=delivery.telegram_message_id,
                                             kind="media_edited" if content_type != "text" else "edited")
            return
        except TelegramError as exc:
            log_telegram_error(LOGGER, "delivery.tombstone_edit", exc, aggregate=self._aggregate_logger,
                               recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id, content_type=content_type)
            pass
        if content_type in {"photo", "video", "animation", "document"}:
            try:
                await self._bot.edit_message_caption(
                    chat_id=delivery.recipient_id,
                    message_id=delivery.telegram_message_id,
                    caption="<i>Message removed.</i>",
                    parse_mode="HTML",
                )
                self.store.mark_delivery_deleted(
                    delivery.id, tombstone_message_id=delivery.telegram_message_id, kind="caption_edited")
                return
            except TelegramError as exc:
                log_telegram_error(LOGGER, "delivery.tombstone_caption", exc, aggregate=self._aggregate_logger,
                                   recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id)
                pass
        try:
            await self._bot.delete_message(delivery.recipient_id, delivery.telegram_message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "delivery.tombstone_delete", exc, aggregate=self._aggregate_logger,
                               recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id)
            pass
        self.store.mark_delivery_deleted(
            delivery.id, tombstone_message_id=None, kind="deleted")

    def _message_deleted(self, message_id: int) -> bool:
        message = self.store.get_message(message_id)
        return bool(message and message.deleted)

    async def _respect_global_rate(self) -> None:
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            min_gap = 1.0 / max(1.0, self.rate_per_second)
            now = loop.time()
            wait = self._last_send + min_gap - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = loop.time()

    async def _respect_recipient_rate(self, recipient_id: int) -> None:
        loop = asyncio.get_running_loop()
        pause_wait = self._recipient_pause_until[recipient_id] - loop.time()
        if pause_wait > 0:
            await asyncio.sleep(pause_wait)
        min_gap = 1.0 / max(0.1, self.per_recipient_rate_per_second)
        now = loop.time()
        wait = self._recipient_last_send[recipient_id] + min_gap - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._recipient_last_send[recipient_id] = loop.time()

    async def _pause_recipient_for_retry_after(self, recipient_id: int, retry_after: float) -> None:
        loop = asyncio.get_running_loop()
        pause_until = loop.time() + max(1.0, retry_after)
        self._recipient_pause_until[recipient_id] = max(self._recipient_pause_until[recipient_id], pause_until)
        await asyncio.sleep(max(1.0, retry_after))

    def _inactive_drop(self, recipient: User, item: DeliveryItem) -> bool:
        if item.is_system or recipient.is_mod_or_admin:
            return False
        period_days = float(self.config.get("inactivity.period_days", 4) or 4)
        last = parse_dt(recipient.last_activity)
        if not last or now_utc() - last <= timedelta(days=period_days):
            self.store.inactive_notified.discard(recipient.telegram_id)
            return False
        if recipient.telegram_id not in self.store.inactive_notified and self._bot:
            self.store.inactive_notified.add(recipient.telegram_id)
            asyncio.create_task(self._bot.send_message(
                recipient.telegram_id, "You are inactive. Text is paused and media is sampled until you use the bot again."))
        if item.content_type == "text":
            return True
        chance = float(self.config.get(
            "inactivity.non_system_receive_chance", 0.05) or 0.05)
        return random.random() > chance

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
                        photo=InputFile(BytesIO(preview), filename="blurred.jpg"),
                        caption=caption,
                        **kwargs,
                    )
                    if sent.photo:
                        self.media.set_blurred_file_id(item.message_id, sent.photo[-1].file_id)
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
        prior = self.store.delivery_for_recipient(
            item.reply_to_message_id, recipient_id)
        if prior:
            return prior.telegram_message_id
        original = self.store.get_message(item.reply_to_message_id)
        if original and original.sender_id == recipient_id and original.source_chat_id == recipient_id:
            return original.source_message_id
        return None


def _is_reply_rejection(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "reply" in text and ("not found" in text or "message to be replied" in text or "invalid" in text)


def _render_text(text: str | None, is_system: bool, *, fallback: str | None = None, trusted_html: bool = False) -> str | None:
    value = text or fallback
    if value is None:
        return None
    if is_system:
        if trusted_html:
            return f"<i>{value}</i>"
        return f"<i>{html_escape(value)}</i>"
    return value
