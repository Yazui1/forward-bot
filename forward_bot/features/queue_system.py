from __future__ import annotations

import asyncio
import random
import time
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut

from forward_bot.features.credits import interpolate_loss_rate
from forward_bot.messages import Messages as Msg
from forward_bot.utils import as_utc

logger = logging.getLogger(__name__)


@dataclass
class DeliveryItem:
    priority: int
    message_id: int
    sender_id: int
    recipient_id: int
    content_type: str
    text_content: str | None
    media_file_id: str | None
    media_kind: str | None
    thumbnail_file_id: str | None
    is_system: bool
    reply_to_message_id: int | None = None
    include_remove_button: bool = False
    parse_mode: str | None = None
    urgent: bool = False


class DeliveryQueue:
    def __init__(self, repo: Any, cfg: dict[str, Any], media_service: Any | None = None) -> None:
        self.repo = repo
        self.cfg = cfg
        self.media_service = media_service
        self.queue: asyncio.PriorityQueue[tuple[int, int, DeliveryItem]] = asyncio.PriorityQueue()
        self._counter = 0
        self._last_send = 0.0
        self._min_interval = 1 / max(1, int(cfg["delivery"]["telegram_rate_limit_per_second"]))
        self._running = False
        self._bot: Any | None = None
        self._inactivity_notified: set[int] = set()
        self._recipient_locks: dict[int, asyncio.Lock] = {}
        self._recipient_pending: dict[int, deque[DeliveryItem]] = {}
        self._recipient_queued: set[int] = set()
        self._recipient_queue_lock = asyncio.Lock()

    def update_config(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._min_interval = 1 / max(1, int(cfg["delivery"]["telegram_rate_limit_per_second"]))

    async def enqueue_batch(
        self,
        message_id: int,
        sender_id: int,
        recipients: list[Any],
        content_type: str,
        text_content: str | None,
        media_file_id: str | None,
        media_kind: str | None,
        thumbnail_file_id: str | None = None,
        is_system: bool = False,
        reply_to_message_id: int | None = None,
        include_remove_button: bool = False,
        parse_mode: str | None = None,
        urgent: bool | None = None,
    ) -> None:
        is_urgent = is_system if urgent is None else urgent
        logger.debug(
            "Enqueue delivery batch message_id=%s sender_id=%s recipients=%s content_type=%s is_system=%s urgent=%s reply_to=%s",
            message_id,
            sender_id,
            len(recipients),
            content_type,
            is_system,
            is_urgent,
            reply_to_message_id,
        )
        items = self._build_batch_items(
            message_id=message_id,
            sender_id=sender_id,
            recipients=recipients,
            content_type=content_type,
            text_content=text_content,
            media_file_id=media_file_id,
            media_kind=media_kind,
            thumbnail_file_id=thumbnail_file_id,
            is_system=is_system,
            reply_to_message_id=reply_to_message_id,
            include_remove_button=include_remove_button,
            parse_mode=parse_mode,
            urgent=is_urgent,
        )
        if is_urgent:
            await self._enqueue_urgent_items(items)
            return
        await self._enqueue_items(items)

    def _build_batch_items(
        self,
        message_id: int,
        sender_id: int,
        recipients: list[Any],
        content_type: str,
        text_content: str | None,
        media_file_id: str | None,
        media_kind: str | None,
        thumbnail_file_id: str | None = None,
        is_system: bool = False,
        reply_to_message_id: int | None = None,
        include_remove_button: bool = False,
        parse_mode: str | None = None,
        urgent: bool = False,
    ) -> list[DeliveryItem]:
        items: list[DeliveryItem] = []
        for user in recipients:
            item = DeliveryItem(
                priority=self._priority_for(user, urgent),
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=user.telegram_id,
                content_type=content_type,
                text_content=text_content,
                media_file_id=media_file_id,
                media_kind=media_kind,
                thumbnail_file_id=thumbnail_file_id,
                is_system=is_system,
                reply_to_message_id=reply_to_message_id,
                include_remove_button=include_remove_button,
                parse_mode=parse_mode,
                urgent=urgent,
            )
            items.append(item)
        return items

    def _priority_for(self, user: Any, urgent: bool) -> int:
        if urgent:
            return -1_000_000_000
        active_window_seconds = int(
            float(self.cfg["delivery"]["active_window_hours"]) * 3600
        )
        if not user.last_activity:
            return active_window_seconds * 2
        try:
            last_active = as_utc(user.last_activity)
        except ValueError:
            return active_window_seconds * 2
        age_seconds = max(
            0,
            int((datetime.now(timezone.utc) - last_active).total_seconds()),
        )
        if age_seconds > active_window_seconds:
            return active_window_seconds + age_seconds
        return age_seconds

    async def _enqueue_items(self, items: list[DeliveryItem]) -> None:
        for item in items:
            await self._enqueue_ordered_item(item)

    async def _enqueue_urgent_items(self, items: list[DeliveryItem]) -> None:
        for item in items:
            if self._bot is not None:
                logger.debug(
                    "Dispatching urgent delivery immediately message_id=%s recipient_id=%s",
                    item.message_id,
                    item.recipient_id,
                )
                asyncio.create_task(self._send_urgent(self._bot, item))
            else:
                self._counter += 1
                await self.queue.put((item.priority, self._counter, item))

    async def _enqueue_ordered_item(self, item: DeliveryItem) -> None:
        async with self._recipient_queue_lock:
            pending = self._recipient_pending.setdefault(
                item.recipient_id, deque()
            )
            pending.append(item)
            if item.recipient_id not in self._recipient_queued:
                await self._queue_next_recipient_item_locked(item.recipient_id)

    async def _queue_next_recipient_item_locked(self, recipient_id: int) -> None:
        pending = self._recipient_pending.get(recipient_id)
        if not pending:
            self._recipient_pending.pop(recipient_id, None)
            self._recipient_queued.discard(recipient_id)
            return
        item = pending.popleft()
        self._recipient_queued.add(recipient_id)
        self._counter += 1
        await self.queue.put((item.priority, self._counter, item))

    async def _complete_ordered_item(self, item: DeliveryItem) -> None:
        if item.urgent:
            return
        async with self._recipient_queue_lock:
            self._recipient_queued.discard(item.recipient_id)
            await self._queue_next_recipient_item_locked(item.recipient_id)

    async def worker(self, bot: Any) -> None:
        if self._running:
            return
        self._running = True
        self._bot = bot
        logger.debug("Delivery worker started")
        while True:
            _, _, item = await self.queue.get()
            try:
                await self._send_with_backoff(bot, item)
            except Exception:
                logger.exception(
                    "Unhandled delivery worker error message_id=%s recipient_id=%s",
                    item.message_id,
                    item.recipient_id,
                )
            finally:
                await self._complete_ordered_item(item)
                self.queue.task_done()

    async def _send_urgent(self, bot: Any, item: DeliveryItem) -> None:
        try:
            await self._send_with_backoff(bot, item)
        except Exception:
            logger.exception(
                "Unhandled urgent delivery error message_id=%s recipient_id=%s",
                item.message_id,
                item.recipient_id,
            )

    async def _send_with_backoff(self, bot: Any, item: DeliveryItem) -> None:
        lock = self._recipient_locks.setdefault(item.recipient_id, asyncio.Lock())
        async with lock:
            await self._send_with_backoff_locked(bot, item)

    async def _send_with_backoff_locked(self, bot: Any, item: DeliveryItem) -> None:
        retried_without_reply = False
        while True:
            try:
                if not item.urgent:
                    await self._respect_api_interval()
                await self._deliver(bot, item)
                return
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 0.1)
            except TimedOut:
                await asyncio.sleep(0.5)
            except Forbidden:
                logger.info(
                    "Recipient unavailable, marking left recipient_id=%s",
                    item.recipient_id,
                )
                await self.repo.mark_left(item.recipient_id)
                return
            except BadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.info(
                        "Recipient chat not found, marking left recipient_id=%s",
                        item.recipient_id,
                    )
                    await self.repo.mark_left(item.recipient_id)
                    return
                if item.reply_to_message_id is not None and not retried_without_reply:
                    retried_without_reply = True
                    logger.debug(
                        "Delivery failed with reply target, retrying without reply message_id=%s recipient_id=%s error=%s",
                        item.message_id,
                        item.recipient_id,
                        e,
                    )
                    item.reply_to_message_id = None
                    continue
                logger.warning(
                    "Dropping delivery after Telegram bad request message_id=%s recipient_id=%s error=%s",
                    item.message_id,
                    item.recipient_id,
                    e,
                )
                return
            except TelegramError as e:
                logger.warning(
                    "Dropping delivery after Telegram error message_id=%s recipient_id=%s error=%s",
                    item.message_id,
                    item.recipient_id,
                    e,
                )
                return
            except Exception:
                logger.exception(
                    "Dropping delivery after unexpected error message_id=%s recipient_id=%s",
                    item.message_id,
                    item.recipient_id,
                )
                return

    async def _deliver(self, bot: Any, item: DeliveryItem) -> None:
        recipient = await self.repo.get_user(item.recipient_id)
        if recipient is None or recipient.is_banned or not recipient.has_started:
            logger.debug(
                "Skipping ineligible recipient message_id=%s recipient_id=%s exists=%s banned=%s started=%s",
                item.message_id,
                item.recipient_id,
                recipient is not None,
                bool(recipient and recipient.is_banned),
                bool(recipient and recipient.has_started),
            )
            return
        if await self._drop_due_to_inactivity(bot, recipient, item):
            logger.debug("Dropped due to inactivity message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
            return

        reply_markup = None
        if item.include_remove_button and not item.is_system and recipient.vote_buttons_enabled:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(Msg.VOTE_TO_REMOVE_BUTTON, callback_data=f"rm:{item.message_id}")]]
            )
        reply_to_message_id = None
        if item.reply_to_message_id is not None:
            if await self.repo.get_message(item.reply_to_message_id) is None:
                return
            reply_to_message_id = await self.repo.delivery_message_for_recipient(
                item.reply_to_message_id,
                item.recipient_id,
            )
            if reply_to_message_id is None:
                return

        if item.content_type == "text":
            is_blurred = False
            sent = await bot.send_message(
                chat_id=item.recipient_id,
                text=item.text_content or "",
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                parse_mode=item.parse_mode,
            )
        elif item.content_type == "photo" and item.media_file_id:
            if (not recipient.is_admin and not recipient.is_moderator) and self._should_blur(recipient.credits):
                is_blurred = True
                blurred = None
                if self.media_service is not None:
                    blurred = await self.media_service.blur_photo(bot, item.media_file_id)
                caption = self._blur_caption(item.text_content, recipient.credits)
                if blurred is not None:
                    logger.info("Blurred photo delivery message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
                    sent = await bot.send_photo(
                        chat_id=item.recipient_id,
                        photo=blurred,
                        caption=caption,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        parse_mode=item.parse_mode,
                    )
                else:
                    logger.info("Blurred photo fallback text message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
                    sent = await bot.send_message(
                        chat_id=item.recipient_id,
                        text=caption,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        parse_mode=item.parse_mode,
                    )
            else:
                is_blurred = False
                sent = await bot.send_photo(
                    chat_id=item.recipient_id,
                    photo=item.media_file_id,
                    caption=item.text_content or "",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=item.parse_mode,
                )
        elif item.content_type in {"video", "animation"} and item.media_file_id:
            if (not recipient.is_admin and not recipient.is_moderator) and self._should_blur(recipient.credits):
                is_blurred = True
                logger.info("Blurred %s delivery message_id=%s recipient_id=%s", item.content_type, item.message_id, item.recipient_id)
                sent = await self._send_blurred_thumbnail_or_notice(bot, item, reply_markup, reply_to_message_id, recipient.credits)
            elif item.content_type == "video":
                is_blurred = False
                sent = await bot.send_video(
                    chat_id=item.recipient_id,
                    video=item.media_file_id,
                    caption=item.text_content or "",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=item.parse_mode,
                )
            else:
                is_blurred = False
                sent = await bot.send_animation(
                    chat_id=item.recipient_id,
                    animation=item.media_file_id,
                    caption=item.text_content or "",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=item.parse_mode,
                )
        elif item.content_type == "video_note" and item.media_file_id:
            if (not recipient.is_admin and not recipient.is_moderator) and self._should_blur(recipient.credits):
                is_blurred = True
                logger.info("Blurred video_note delivery message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
                sent = await self._send_blurred_thumbnail_or_notice(bot, item, reply_markup, reply_to_message_id, recipient.credits)
            else:
                is_blurred = False
                sent = await bot.send_video_note(
                    chat_id=item.recipient_id,
                    video_note=item.media_file_id,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                )
        elif item.content_type == "sticker" and item.media_file_id:
            if (not recipient.is_admin and not recipient.is_moderator) and self._should_blur(recipient.credits):
                is_blurred = True
                logger.info("Blurred sticker delivery message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
                sent = await self._send_blurred_thumbnail_or_notice(bot, item, reply_markup, reply_to_message_id, recipient.credits)
            else:
                is_blurred = False
                sent = await bot.send_sticker(
                    chat_id=item.recipient_id,
                    sticker=item.media_file_id,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                )
        elif item.content_type == "document" and item.media_file_id:
            if (
                item.thumbnail_file_id
                and (not recipient.is_admin and not recipient.is_moderator)
                and self._should_blur(recipient.credits)
            ):
                is_blurred = True
                logger.info("Blurred document delivery message_id=%s recipient_id=%s", item.message_id, item.recipient_id)
                sent = await self._send_blurred_thumbnail_or_notice(bot, item, reply_markup, reply_to_message_id, recipient.credits)
            else:
                is_blurred = False
                sent = await bot.send_document(
                    chat_id=item.recipient_id,
                    document=item.media_file_id,
                    caption=item.text_content or "",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode=item.parse_mode,
                )
        else:
            is_blurred = False
            sent = await bot.send_message(
                chat_id=item.recipient_id,
                text=item.text_content or "",
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                parse_mode=item.parse_mode,
            )

        await self.repo.add_delivery(item.message_id, item.recipient_id, sent.message_id, is_blurred=is_blurred)
        logger.debug(
            "Delivered message_id=%s recipient_id=%s telegram_message_id=%s blurred=%s",
            item.message_id,
            item.recipient_id,
            sent.message_id,
            is_blurred,
        )

    async def _drop_due_to_inactivity(self, bot: Any, recipient: Any, item: DeliveryItem) -> bool:
        last_activity = recipient.last_activity
        if not last_activity:
            return False
        try:
            dt = as_utc(last_activity)
        except ValueError:
            return False
        inactive_days = int(self.cfg["inactivity"]["period_days"])
        inactive = datetime.now(timezone.utc) - dt > timedelta(days=inactive_days)
        if not inactive:
            self._inactivity_notified.discard(recipient.telegram_id)
            return False
        if item.is_system:
            return False
        await self._send_inactivity_notice_once(bot, recipient.telegram_id)
        if item.content_type == "text":
            return True
        chance = float(self.cfg["inactivity"]["non_system_receive_chance"])
        return random.random() > chance

    async def _send_inactivity_notice_once(self, bot: Any, recipient_id: int) -> None:
        if recipient_id in self._inactivity_notified:
            return
        self._inactivity_notified.add(recipient_id)
        try:
            await bot.send_message(chat_id=recipient_id, text=Msg.INACTIVITY_NOTICE)
        except Exception:
            logger.debug("Failed to send inactivity notice recipient_id=%s", recipient_id, exc_info=True)

    def _should_blur(self, credits: float) -> bool:
        loss_rate = interpolate_loss_rate(self.cfg["loss_rate"]["schedule"], credits)
        return random.random() < max(0.0, min(1.0, loss_rate))

    def _blur_caption(self, original: str | None, credits: float) -> str:
        loss_rate = interpolate_loss_rate(self.cfg["loss_rate"]["schedule"], credits) * 100.0
        notice = Msg.blurred_notice(loss_rate)
        return f"{original}\n\n{notice}" if original else notice

    async def _send_blurred_thumbnail_or_notice(
        self,
        bot: Any,
        item: DeliveryItem,
        reply_markup: Any,
        reply_to_message_id: int | None,
        recipient_credits: float,
    ) -> Any:
        blurred = None
        if self.media_service is not None and item.thumbnail_file_id:
            blurred = await self.media_service.blur_image(bot, item.thumbnail_file_id)
        if blurred is not None:
            return await bot.send_photo(
                chat_id=item.recipient_id,
                photo=blurred,
                caption=self._blur_caption(item.text_content, recipient_credits),
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                parse_mode=item.parse_mode,
            )
        return await bot.send_message(
            chat_id=item.recipient_id,
            text=self._blur_caption(item.text_content, recipient_credits),
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            parse_mode=item.parse_mode,
        )

    async def _respect_api_interval(self) -> None:
        now = time.time()
        delta = now - self._last_send
        if delta < self._min_interval:
            await asyncio.sleep(self._min_interval - delta)
        self._last_send = time.time()
