from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import BadRequest, TelegramError

from forward_bot.cache.transient import TransientStore
from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.features.tombstones import mark_for_moderation_action, remove_message
from forward_bot.logging_utils import log_telegram_error


LOGGER = logging.getLogger(__name__)


async def vote_to_remove(
    bot: Bot,
    repo: Repository,
    store: TransientStore,
    config: Config,
    message_id: int,
    voter: User,
) -> tuple[bool, str]:
    msg = store.get_message(message_id)
    if not msg:
        return False, "Message is not in cache anymore."
    if msg.sender_id == voter.telegram_id:
        return False, "You cannot vote to remove your own message."
    if voter.active_cooldown_seconds > 0:
        return False, "You cannot vote while in cooldown."
    min_percentile = config.get("vote_to_remove.voter_min_top_credit_percentile", None)
    if min_percentile is not None:
        cutoff = repo.credit_percentile_cutoff(float(min_percentile))
        if voter.credits < cutoff:
            return False, "You need enough credits relative to other users to remove-vote."
    cd = int(config.get("vote_to_remove.user_vote_cooldown_seconds", 300) or 300)
    left = store.latest_remove_vote_seconds_left(voter.telegram_id, cd)
    if left > 0:
        return False, f"Remove vote cooldown: {left}s."
    user_window = int(config.get("vote_to_remove.user_remove_cooldown_seconds", 3600) or 3600)
    user_limit = int(config.get("vote_to_remove.user_remove_limit", 3) or 3)
    if store.recent_remove_votes_by_user(voter.telegram_id, user_window) >= user_limit:
        return False, "You reached the remove-vote limit."
    global_window = int(config.get("vote_to_remove.global_cooldown_seconds", 3600) or 3600)
    global_limit = int(config.get("vote_to_remove.global_limit", 6) or 6)
    if store.recent_global_removals(global_window) >= global_limit:
        return False, "Community remove-vote limit reached."
    ok, count = store.add_remove_vote(message_id, voter.telegram_id)
    if not ok:
        return False, f"You already voted. Current votes: {count}."
    repo.touch_activity(voter.telegram_id)
    queue = getattr(store, "delivery_queue", None)
    if queue and hasattr(queue, "on_user_activity"):
        queue.on_user_activity(voter.telegram_id)
    await mark_for_moderation_action(bot, repo, store, config, message_id)
    threshold = int(config.get("vote_to_remove.threshold", 3) or 3)
    if count < threshold:
        return True, f"Remove vote recorded ({count}/{threshold})."
    voters = list(store.remove_votes.get(message_id, set()))
    voter_reply_targets = _voter_reply_targets(store, message_id, voters)
    store.record_global_removal()
    await remove_message(
        bot,
        repo,
        store,
        config,
        message_id,
        reason="community remove vote threshold reached",
        voter_ids=voters,
    )
    await _notify_voters(bot, store, repo, voter_reply_targets, "Remove vote threshold reached. Message removed.")
    await _remove_collateral(bot, repo, store, config, message_id)
    return True, "Remove vote threshold reached. Message removed."


def _voter_reply_targets(store: TransientStore, message_id: int, voter_ids: list[int]) -> dict[int, int | None]:
    targets: dict[int, int | None] = {}
    deliveries = {delivery.recipient_id: delivery for delivery in store.deliveries_for_message(message_id)}
    for voter_id in voter_ids:
        delivery = deliveries.get(voter_id)
        targets[voter_id] = delivery.telegram_message_id if delivery else None
    return targets


async def _notify_voters(bot: Bot, store: TransientStore, repo: Repository, reply_targets: dict[int, int | None], text: str) -> None:
    for voter_id, reply_to in reply_targets.items():
        if not reply_to:
            LOGGER.info(
                "telegram remove_vote.notify_voter skipped missing reply target fields=%s",
                {"user_id": voter_id},
            )
            continue
        try:
            await bot.send_message(
                voter_id,
                text,
                reply_to_message_id=reply_to,
            )
        except BadRequest as exc:
            if reply_to and _is_reply_rejection(exc):
                queue = getattr(store, "delivery_queue", None)
                aggregate = getattr(queue, "_aggregate_logger", None)
                log_telegram_error(LOGGER, "remove_vote.notify_voter_reply_rejected", exc, aggregate=aggregate, repo=repo, user_id=voter_id, reply_to=reply_to)
                continue
            queue = getattr(store, "delivery_queue", None)
            aggregate = getattr(queue, "_aggregate_logger", None)
            log_telegram_error(LOGGER, "remove_vote.notify_voter", exc, aggregate=aggregate, repo=repo, user_id=voter_id, reply_to=reply_to)
        except TelegramError as exc:
            queue = getattr(store, "delivery_queue", None)
            aggregate = getattr(queue, "_aggregate_logger", None)
            log_telegram_error(LOGGER, "remove_vote.notify_voter", exc, aggregate=aggregate, repo=repo, user_id=voter_id, reply_to=reply_to)
            pass


def _is_reply_rejection(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "reply" in text and ("not found" in text or "message to be replied" in text or "invalid" in text)


async def _remove_collateral(bot: Bot, repo: Repository, store: TransientStore, config: Config, pivot_id: int) -> None:
    pivot = store.get_message(pivot_id)
    if not pivot or not pivot.sender_id:
        return
    amount = int(config.get("vote_to_remove.collateral_remove_amount", 0) or 0)
    if amount <= 0:
        return
    before = [
        m for m in store.messages.values()
        if m.sender_id == pivot.sender_id and m.id < pivot_id and not m.deleted
    ]
    after = [
        m for m in store.messages.values()
        if m.sender_id == pivot.sender_id and m.id > pivot_id and not m.deleted
    ]
    before.sort(key=lambda m: m.id, reverse=True)
    after.sort(key=lambda m: m.id)
    neighbors = []
    while len(neighbors) < amount and (before or after):
        if before:
            neighbors.append(before.pop(0))
            if len(neighbors) >= amount:
                break
        if after:
            neighbors.append(after.pop(0))
    for msg in neighbors:
        await remove_message(bot, repo, store, config, msg.id, reason="collateral removal", notify_sender=False)
