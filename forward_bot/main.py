from __future__ import annotations

import argparse
import asyncio
import signal
import logging
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

try:
    from telegram.ext import MessageReactionHandler
except Exception:  # pragma: no cover
    MessageReactionHandler = None

from forward_bot.cache.transient import TransientStore
from forward_bot.commands.help_registry import HelpRegistry
from forward_bot.commands.mod_commands import register_mod_commands
from forward_bot.commands.user_commands import register_user_commands
from forward_bot.config import Config
from forward_bot.db.migration import migrate_legacy_database
from forward_bot.db.repository import Repository
from forward_bot.features.background import cleanup_worker, daily_tax_worker, tips_worker
from forward_bot.features.media import AIClassifier, MediaService
from forward_bot.features.queue_system import DeliveryQueue
from forward_bot.features.rate_limit import RateLimiter
from forward_bot.features.tagging import TaggingPipeline
from forward_bot.handlers.message_handlers import handle_callback, handle_edited_message, handle_message, handle_reaction
from forward_bot.logging_utils import AggregateLogger, aggregate_log_worker, configure_logging
from forward_bot.logging_utils import log_telegram_error


LOGGER = logging.getLogger(__name__)


async def log_update_error(update: object, context) -> None:
    aggregate = context.application.bot_data.get("aggregate_logger") if context and context.application else None
    error = getattr(context, "error", None)
    if isinstance(error, TelegramError):
        log_telegram_error(LOGGER, "update_handler", error, aggregate=aggregate)
        return
    if isinstance(error, BaseException):
        LOGGER.error("update handler failed update=%r", update, exc_info=(type(error), error, error.__traceback__))
    else:
        LOGGER.error("update handler failed update=%r error=%r", update, error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    return parser.parse_args()


async def run(config_path: str = "config.yml") -> None:
    config = Config.load(config_path)
    configure_logging(config.get("logging.level", "INFO"))
    db_path = _resolve_config_path(config, config.get("database.path", "./data/bot.db"))
    migration_source = config.get("database.migrate_from")
    migrated_about_text = None
    if migration_source:
        result = migrate_legacy_database(_resolve_config_path(config, migration_source), db_path)
        migrated_about_text = result.about_text
        if result.migrated:
            LOGGER.info("legacy DB migration complete: %s", result.counts)
        elif result.reason not in {"no source configured", "source does not exist"}:
            LOGGER.info("legacy DB migration skipped: %s", result.reason)

    token = str(config.get("bot.token", "") or "")
    if not token or token.startswith("${"):
        raise RuntimeError("Bot token is missing. Set BOT_TOKEN or configure bot.token.")

    repo = Repository(db_path, about_text=migrated_about_text or str(config.get("about.text", "")))
    repo.sync_admin_ids(config.get("bot.admin_ids", []) or [])
    store = TransientStore(
        int(config.get("cache.transient_message_ttl_hours", 72) or 72),
        int(config.get("cache.sender_metadata_max_size", 86400) or 86400),
        int(config.get("cache.sender_metadata_ttl_seconds", 86400) or 86400),
    )
    media = MediaService()
    ai = AIClassifier(config.section("ai"))
    tagger = TaggingPipeline(config, repo, ai)
    rate_limiter = RateLimiter(
        int(config.get("rate_limits.message_send_limit", 8) or 8),
        int(config.get("rate_limits.window_seconds", 30) or 30),
    )
    aggregate_logger = AggregateLogger()
    queue = DeliveryQueue(config, repo, store, media, aggregate_logger=aggregate_logger)
    setattr(store, "delivery_queue", queue)
    config_ref = {"config": config}

    builder = ApplicationBuilder().token(token)
    builder = builder.connection_pool_size(int(config.get("delivery.connection_pool_size", 64) or 64))
    builder = builder.pool_timeout(float(config.get("delivery.pool_timeout_seconds", 30) or 30))
    app = builder.build()
    app.bot_data.update(
        repo=repo,
        store=store,
        media=media,
        ai=ai,
        tagger=tagger,
        rate_limiter=rate_limiter,
        queue=queue,
        aggregate_logger=aggregate_logger,
        config_ref=config_ref,
    )

    registry = HelpRegistry()
    register_user_commands(registry)
    register_mod_commands(registry)
    app.bot_data["help_registry"] = registry
    registry.register(app)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited_message), group=1)
    app.add_handler(CallbackQueryHandler(handle_callback), group=1)
    if MessageReactionHandler is not None:
        app.add_handler(MessageReactionHandler(handle_reaction), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_message), group=2)
    app.add_error_handler(log_update_error)

    stop_event = asyncio.Event()
    background_tasks: list[asyncio.Task] = []

    def _stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await app.initialize()
    LOGGER.info("starting bot with %d registered commands", len(registry.commands))
    await queue.start(app.bot)
    background_tasks.extend(
        [
            asyncio.create_task(daily_tax_worker(app.bot, repo, config_ref, stop_event), name="daily-tax"),
            asyncio.create_task(tips_worker(app.bot, repo, config_ref, stop_event, aggregate_logger), name="tips"),
            asyncio.create_task(cleanup_worker(app.bot, repo, store, media, stop_event, aggregate_logger), name="cleanup"),
            asyncio.create_task(
                aggregate_log_worker(
                    aggregate_logger,
                    int(config.get("logging.aggregate_interval_seconds", 60) or 60),
                    stop_event,
                ),
                name="aggregate-log",
            ),
        ]
    )
    await app.start()
    if not app.updater:
        raise RuntimeError("Application updater is unavailable.")
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        stop_event.set()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await queue.stop()
        await app.shutdown()


def _resolve_config_path(config: Config, value: str | Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return (config.path.parent / path).resolve()
