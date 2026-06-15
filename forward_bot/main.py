from __future__ import annotations

import asyncio
import argparse
import logging
from pathlib import Path

from telegram.ext import Application

from forward_bot.cache import EphemeralState, SenderMetadataCache
from forward_bot.commands import register_mod_commands, register_user_commands
from forward_bot.config import Config
from forward_bot.db import Repository, init_schema
from forward_bot.features import AIClassifier, DeliveryQueue, MediaService, RateLimiter, TaggingPipeline, daily_tax_worker, tips_worker
from forward_bot.handlers import register_message_handlers


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("forward_bot").setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the anonymous forwarding Telegram bot.")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yml",
        help="Path to the config.yml file. Defaults to ./config.yml.",
    )
    return parser.parse_args()


async def run(config_path: str = "config.yml") -> None:
    configure_logging()
    cfg = Config(config_path).data
    db_path = cfg["database"]["path"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    await init_schema(db_path, str(cfg["bot"].get("global_salt", "")))
    repo = Repository(
        db_path,
        transient_ttl_seconds=int(float(cfg["cache"].get("transient_message_ttl_hours", 72)) * 3600),
    )
    await repo.sync_admin_ids(set(int(x) for x in cfg["bot"].get("admin_ids", [])))

    sender_cache = SenderMetadataCache(
        max_size=int(cfg["cache"]["sender_metadata_max_size"]),
        ttl_seconds=int(cfg["cache"]["sender_metadata_ttl_seconds"]),
    )
    state = EphemeralState(ttl_seconds=int(cfg["cache"]["pending_state_ttl_seconds"]))
    rate_limiter = RateLimiter(
        limit=int(cfg["rate_limits"]["message_send_limit"]),
        window_seconds=int(cfg["rate_limits"]["window_seconds"]),
    )
    media_service = MediaService()
    queue = DeliveryQueue(repo=repo, cfg=cfg, media_service=media_service)
    ai_classifier = AIClassifier.from_config(cfg)
    tagger = TaggingPipeline(
        blocked_terms=list(cfg["tagging"]["blocked_terms"]),
        questionable_terms=list(cfg["tagging"]["questionable_terms"]),
        potentially_unwanted_terms=list(cfg["tagging"].get("potentially_unwanted_terms", [])),
        ai_classifier=ai_classifier,
    )

    app = (
        Application.builder()
        .token(cfg["bot"]["token"])
        .connection_pool_size(int(cfg.get("delivery", {}).get("connection_pool_size", 64)))
        .pool_timeout(float(cfg.get("delivery", {}).get("pool_timeout_seconds", 30.0)))
        .build()
    )
    app.bot_data["repo"] = repo
    app.bot_data["cfg"] = cfg
    app.bot_data["config_path"] = config_path
    app.bot_data["queue"] = queue
    app.bot_data["media_service"] = media_service
    app.bot_data["tagger"] = tagger
    app.bot_data["ai_classifier"] = ai_classifier
    app.bot_data["sender_cache"] = sender_cache
    app.bot_data["state"] = state
    app.bot_data["rate_limiter"] = rate_limiter
    register_user_commands(app, repo, cfg)
    register_mod_commands(app, repo, cfg, sender_cache)
    register_message_handlers(app, repo, cfg, rate_limiter, queue, tagger, state, sender_cache)

    await app.initialize()
    await app.start()
    workers = int(cfg["delivery"].get("worker_count", 1))
    for _ in range(max(1, workers)):
        app.create_task(queue.worker(app.bot))
    app.create_task(daily_tax_worker(repo, cfg))
    app.create_task(tips_worker(app.bot, repo, cfg))
    await app.updater.start_polling(allowed_updates=["message", "edited_message", "callback_query", "message_reaction"])
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args.config))
