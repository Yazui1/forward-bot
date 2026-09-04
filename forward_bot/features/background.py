from __future__ import annotations

import asyncio
import logging
import random

from telegram import Bot
from telegram.error import Forbidden, TelegramError

from forward_bot.cache.transient import TransientStore
from forward_bot.config import Config
from forward_bot.db.repository import Repository
from forward_bot.features.credits import maybe_apply_negative_cooldown, tax_rate
from forward_bot.features.media import MediaService
from forward_bot.logging_utils import AggregateLogger, log_telegram_error


LOGGER = logging.getLogger(__name__)


async def daily_tax_worker(bot: Bot, repo: Repository, config_ref: dict, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        config: Config = config_ref["config"]
        if config.get("credits.daily_tax_enabled", True):
            user_rates = [
                (user, tax_rate(config, user.credits))
                for user in repo.list_users()
                if not user.is_banned
            ]
            for amount, updated in repo.apply_daily_taxes(user_rates):
                if amount:
                    maybe_apply_negative_cooldown(repo, config, updated)
        await _sleep(stop_event, int(config.get("credits.daily_tax_check_interval_seconds", 3600) or 3600))


async def tips_worker(bot: Bot, repo: Repository, config_ref: dict, stop_event: asyncio.Event, aggregate_logger: AggregateLogger | None = None) -> None:
    while not stop_event.is_set():
        config: Config = config_ref["config"]
        messages = list(config.get("tips.messages", []) or [])
        if config.get("tips.enabled", False) and messages:
            for user in repo.list_users():
                if not user.has_started or user.is_banned:
                    continue
                try:
                    await bot.send_message(user.telegram_id, random.choice(messages))
                except Forbidden as exc:
                    log_telegram_error(LOGGER, "tips.forbidden", exc, aggregate=aggregate_logger, repo=repo, user_id=user.telegram_id)
                    repo.mark_left(user.telegram_id)
                except TelegramError as exc:
                    log_telegram_error(LOGGER, "tips.send", exc, aggregate=aggregate_logger, repo=repo, user_id=user.telegram_id)
                    pass
        interval = max(60, int(float(config.get("tips.interval_hours", 24) or 24) * 3600))
        await _sleep(stop_event, interval)


async def cleanup_worker(bot: Bot, repo: Repository, store: TransientStore, media: MediaService, stop_event: asyncio.Event, aggregate_logger: AggregateLogger | None = None) -> None:
    while not stop_event.is_set():
        for fight in store.expire_due_fights():
            try:
                await bot.send_message(fight.sender_id, "Fight expired.", reply_to_message_id=fight.command_message_id)
            except TelegramError as exc:
                log_telegram_error(LOGGER, "fight.expire_notify", exc, aggregate=aggregate_logger, repo=repo, user_id=fight.sender_id)
                pass
            if fight.target_message_id:
                try:
                    await bot.edit_message_text(chat_id=fight.target_id, message_id=fight.target_message_id, text="Fight expired.")
                except TelegramError as exc:
                    log_telegram_error(LOGGER, "fight.expire_edit", exc, aggregate=aggregate_logger, repo=repo, user_id=fight.target_id, message_id=fight.target_message_id)
                    pass
        expired_media_ids = set(store.expired_message_ids()) | set(store.expired_confirmation_message_ids())
        store.cleanup()
        for message_id in expired_media_ids:
            media.release(message_id)
        repo.flush_activity()
        await _sleep(stop_event, 300)


async def _sleep(stop_event: asyncio.Event, seconds: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(1, seconds))
    except asyncio.TimeoutError:
        return
