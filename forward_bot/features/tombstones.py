from __future__ import annotations

import logging
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

try:
    from telegram import ReactionTypeEmoji
except Exception:  # pragma: no cover
    ReactionTypeEmoji = None

from forward_bot.cache.transient import TransientDelivery, TransientStore
from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.features.credits import apply_credit, maybe_apply_negative_cooldown
from forward_bot.features.tombstone_media import removed_photo_media
from forward_bot.identity import display_identity_html
from forward_bot.logging_utils import log_telegram_error
from forward_bot.utils import html_escape, round_credits


LOGGER = logging.getLogger(__name__)


async def remove_message(
    bot: Bot,
    repo: Repository,
    store: TransientStore,
    config: Config,
    message_id: int,
    *,
    reason: str,
    remove_for_mods: bool = False,
    notify_sender: bool = True,
    voter_ids: list[int] | None = None,
) -> int:
    msg = store.get_message(message_id)
    if not msg or (msg.deleted and not remove_for_mods) or (msg.removed_for_mods and remove_for_mods):
        return 0
    if remove_for_mods:
        msg.removed_for_mods = True
    else:
        msg.deleted = True
    msg.deletion_reason = reason
    queue = getattr(store, "delivery_queue", None)
    if queue is not None and not remove_for_mods:
        try:
            queue.promote_deleted_message(message_id)
        except Exception:
            pass
    sender = repo.get_user(msg.sender_id) if msg.sender_id else None
    updated = 0
    for delivery in store.deliveries_for_message(message_id):
        user = repo.get_user(delivery.recipient_id)
        if not user or delivery.deleted:
            continue
        if remove_for_mods and not user.is_mod_or_admin:
            continue
        if user.is_mod_or_admin and not remove_for_mods:
            continue
        if await _tombstone_delivery(bot, store, delivery, "<i>Message removed.</i>", content_type=msg.content_type):
            updated += 1
    if notify_sender and sender:
        try:
            await bot.send_message(
                sender.telegram_id,
                f"Your message was removed: {html_escape(reason)}",
                parse_mode="HTML",
                reply_to_message_id=_sender_source_reply(msg),
            )
        except TelegramError as exc:
            log_telegram_error(LOGGER, "tombstone.notify_sender", exc, aggregate=_aggregate(store), repo=repo, user_id=sender.telegram_id, message_id=message_id)
            pass
    await send_mod_notes(bot, repo, store, config, message_id, reason=reason, voter_ids=voter_ids or [])
    return updated


async def mark_for_moderation_action(bot: Bot, repo: Repository, store: TransientStore, config: Config, message_id: int) -> None:
    if ReactionTypeEmoji is None:
        return
    msg = store.get_message(message_id)
    if not msg:
        return
    emoji = str(config.get("moderation.delete_reaction_emoji", "✍️") or "✍️")
    for user in repo.list_users():
        if not user.has_started or not user.is_mod_or_admin:
            continue
        delivery = store.delivery_for_recipient(message_id, user.telegram_id)
        if not delivery or delivery.deleted:
            continue
        try:
            await bot.set_message_reaction(
                chat_id=user.telegram_id,
                message_id=delivery.telegram_message_id,
                reaction=[ReactionTypeEmoji(emoji)],
            )
        except TelegramError as exc:
            log_telegram_error(LOGGER, "moderation.mark_reaction", exc, aggregate=_aggregate(store), repo=repo, user_id=user.telegram_id, message_id=message_id)
            pass


async def _tombstone_delivery(
    bot: Bot,
    store: TransientStore,
    delivery: TransientDelivery,
    text: str,
    *,
    content_type: str = "text",
) -> bool:
    try:
        if content_type == "text":
            await bot.edit_message_text(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, text=text, parse_mode="HTML")
        elif content_type in {"photo", "video", "animation", "document"}:
            await bot.edit_message_media(
                chat_id=delivery.recipient_id,
                message_id=delivery.telegram_message_id,
                media=removed_photo_media(text),
            )
        else:
            raise TelegramError("content type cannot be tombstoned in-place")
        store.mark_delivery_deleted(delivery.id, tombstone_message_id=delivery.telegram_message_id, kind="media_edited" if content_type != "text" else "edited")
        return True
    except TelegramError as exc:
        log_telegram_error(LOGGER, "tombstone.edit", exc, aggregate=_aggregate(store), recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id, content_type=content_type)
        pass
    try:
        await bot.delete_message(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id)
    except TelegramError as exc:
        log_telegram_error(LOGGER, "tombstone.delete", exc, aggregate=_aggregate(store), recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id)
        store.mark_delivery_deleted(delivery.id, tombstone_message_id=None, kind="delete_failed")
        return False
    store.mark_delivery_deleted(delivery.id, tombstone_message_id=None, kind="deleted")
    return True


async def send_mod_notes(
    bot: Bot,
    repo: Repository,
    store: TransientStore,
    config: Config,
    message_id: int,
    *,
    reason: str,
    voter_ids: list[int] | None = None,
) -> None:
    msg = store.get_message(message_id)
    if not msg:
        return
    sender = repo.get_user(msg.sender_id) if msg.sender_id else None
    voter_users = [repo.get_user(voter_id) for voter_id in (voter_ids or sorted(store.remove_votes.get(message_id, set())))]
    existing_notes = list(store.mod_notes.get(message_id, []))
    tombstone_note = _tombstone_note_text(store, msg)
    if existing_notes:
        for recipient_id, note_message_id in existing_notes:
            user = repo.get_user(recipient_id)
            if not user or not user.has_started or not user.is_mod_or_admin:
                continue
            text = _mod_note_text(
                config,
                sender,
                reason,
                [v for v in voter_users if v],
                msg.punishment_confirmed,
                msg.removed_for_mods,
                msg.reverted,
                viewer=user,
                actions=list(msg.metadata.get("mod_actions", [])),
                tombstone_note=tombstone_note,
            )
            await _edit_mod_note(bot, config, user.telegram_id, note_message_id, text, _mod_note_markup(user, msg, bool(voter_users)))
        return
    for user in repo.list_users():
        if not user.has_started or not user.is_mod_or_admin:
            continue
        markup = _mod_note_markup(user, msg, bool(voter_users))
        text = _mod_note_text(
            config,
            sender,
            reason,
            [v for v in voter_users if v],
            msg.punishment_confirmed,
            msg.removed_for_mods,
            msg.reverted,
            viewer=user,
            actions=list(msg.metadata.get("mod_actions", [])),
            tombstone_note=tombstone_note,
        )
        reply_to = None
        delivery = store.delivery_for_recipient(message_id, user.telegram_id)
        if delivery:
            reply_to = delivery.telegram_message_id
        try:
            sent = await bot.send_message(user.telegram_id, text, parse_mode="HTML", reply_to_message_id=reply_to, reply_markup=markup)
            store.add_mod_note(message_id, user.telegram_id, sent.message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "moderation.note_send", exc, aggregate=_aggregate(store), repo=repo, user_id=user.telegram_id, message_id=message_id, reply_to=reply_to)
            if not reply_to:
                continue
            try:
                sent = await bot.send_message(
                    user.telegram_id,
                    text + "\nNote: The referenced message had to be deleted or is unavailable, so this moderation note could not reply to it.",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                store.add_mod_note(message_id, user.telegram_id, sent.message_id)
            except TelegramError as fallback_exc:
                log_telegram_error(LOGGER, "moderation.note_send_fallback", fallback_exc, aggregate=_aggregate(store), repo=repo, user_id=user.telegram_id, message_id=message_id)
                pass


def _mod_note_markup(user: User, msg, has_voters: bool) -> InlineKeyboardMarkup | None:
    buttons = []
    if not msg.punishment_confirmed and not msg.reverted:
        buttons.append(InlineKeyboardButton("Punish", callback_data=f"mconf:{msg.id}:{msg.sender_id or 0}"))
    if not msg.removed_for_mods and not msg.reverted:
        buttons.append(InlineKeyboardButton("Remove for mods", callback_data=f"mrm:{msg.id}:{msg.sender_id or 0}"))
    if has_voters and not msg.punishment_confirmed and not msg.reverted:
        buttons.append(InlineKeyboardButton("Revert", callback_data=f"mrev:{msg.id}:{msg.sender_id or 0}"))
    if user.is_admin and msg.sender_id:
        buttons.append(InlineKeyboardButton("Ban", callback_data=f"mban:{msg.id}:{msg.sender_id}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def _edit_mod_note(bot: Bot, config: Config, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=markup)
        return
    except TelegramError as exc:
        log_telegram_error(LOGGER, "moderation.note_edit_html", exc, chat_id=chat_id, message_id=message_id)
        pass
    plain = _plain_text(text)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=plain, reply_markup=markup)
        return
    except TelegramError as exc:
        log_telegram_error(LOGGER, "moderation.note_edit_plain", exc, chat_id=chat_id, message_id=message_id)
        pass
    emoji = _status_emoji(config, text)
    if emoji and ReactionTypeEmoji is not None:
        try:
            await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji)])
        except TelegramError as exc:
            log_telegram_error(LOGGER, "moderation.note_reaction", exc, chat_id=chat_id, message_id=message_id)
            pass


def _status_emoji(config: Config, text: str) -> str | None:
    statuses = config.section("moderation.status_reactions")
    lowered = text.lower()
    if "reverted" in lowered:
        return statuses.get("reverted")
    if "removed for mods" in lowered:
        return statuses.get("removed")
    if "punished" in lowered:
        return statuses.get("confirmed")
    return statuses.get("pending")


def _plain_text(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _mod_note_text(
    config: Config,
    sender: User | None,
    reason: str,
    voters: list[User],
    punished: bool,
    removed_for_mods: bool,
    reverted: bool,
    *,
    viewer: User,
    actions: list[str] | None = None,
    tombstone_note: str | None = None,
) -> str:
    sender_id = display_identity_html(sender, config, viewer=viewer)
    voter_text = ", ".join(display_identity_html(v, config, viewer=viewer) for v in voters) if voters else "none"
    flags = []
    if punished:
        flags.append("punished")
    if removed_for_mods:
        flags.append("removed for mods")
    if reverted:
        flags.append("reverted")
    flag_text = ", ".join(flags) if flags else "pending"
    text = (
        "<b>Moderation removal</b>\n"
        f"Status: {html_escape(flag_text)}\n"
        f"Reason: {html_escape(reason)}\n"
        f"Sender: {sender_id}\n"
        f"Voters: {voter_text}"
    )
    if tombstone_note:
        text += f"\nNote: {html_escape(tombstone_note)}"
    if actions:
        text += "\nActions:\n" + "\n".join(f"- {html_escape(action)}" for action in actions[-5:])
    return text


def _tombstone_note_text(store: TransientStore, msg) -> str | None:
    if msg.content_type == "text":
        return None
    kinds = {delivery.tombstone_kind for delivery in store.deliveries_for_message(msg.id)}
    if "deleted" in kinds:
        return "The referenced message had to be deleted because Telegram could not tombstone it in place."
    return None


async def punish_sender(bot: Bot, repo: Repository, store: TransientStore, config: Config, message_id: int, moderator_id: int | None = None) -> str:
    msg = store.get_message(message_id)
    if not msg or msg.sender_id is None:
        return "Message is not in cache anymore."
    if msg.punishment_confirmed or msg.reverted:
        return "This moderation action is already resolved."
    sender = repo.get_user(msg.sender_id)
    if not sender:
        return "Sender not found."
    percent = float(config.get("vote_to_remove.punishment_credit_tax_percent", 0.8) or 0.8)
    minimum = float(config.get("vote_to_remove.punishment_credit_minimum", 10.0) or 10.0)
    penalty = -round_credits(max(sender.credits * percent, minimum))
    _, updated = repo.apply_credit_change(sender.telegram_id, penalty, "remove_punishment", daily_caps=None)
    maybe_apply_negative_cooldown(repo, config, updated)
    cooldown = int(config.get("vote_to_remove.punishment_cooldown_seconds", 3600) or 3600)
    if cooldown > 0:
        repo.set_cooldown(sender.telegram_id, cooldown, "moderation punishment", moderator_id, stack=False)
    msg.punishment_confirmed = True
    _append_mod_action(msg, f"Punished sender for {abs(penalty):.2f} credits")
    try:
        await bot.send_message(
            sender.telegram_id,
            f"Moderation confirmed. Penalty: {abs(penalty):.2f} credits. Balance: {updated.credits:.2f}.",
            reply_to_message_id=_sender_source_reply(msg),
        )
    except TelegramError as exc:
        log_telegram_error(LOGGER, "moderation.punish_notify", exc, aggregate=_aggregate(store), repo=repo, user_id=sender.telegram_id, message_id=message_id)
        pass
    await send_mod_notes(bot, repo, store, config, message_id, reason=msg.deletion_reason or "moderation confirmed", voter_ids=list(store.remove_votes.get(message_id, set())))
    return "Punishment applied."


async def remove_for_moderators(bot: Bot, repo: Repository, store: TransientStore, config: Config, message_id: int, moderator_id: int | None = None) -> str:
    msg = store.get_message(message_id)
    if not msg:
        return "Message is not in cache anymore."
    if msg.reverted or msg.removed_for_mods:
        return "This moderation action is already resolved."
    count = await remove_message(bot, repo, store, config, message_id, reason=msg.deletion_reason or "removed for moderators", remove_for_mods=True, notify_sender=False)
    msg.removed_for_mods = True
    _append_mod_action(msg, f"Removed {count} moderator copies")
    await send_mod_notes(bot, repo, store, config, message_id, reason=msg.deletion_reason or "removed for moderators", voter_ids=list(store.remove_votes.get(message_id, set())))
    return f"Removed {count} moderator copies."


async def revert_remove_vote(bot: Bot, repo: Repository, store: TransientStore, config: Config, message_id: int) -> str:
    msg = store.get_message(message_id)
    if not msg:
        return "Message is not in cache anymore."
    if msg.reverted or msg.punishment_confirmed:
        return "This moderation action is already resolved."
    voters = list(store.remove_votes.get(message_id, set()))
    if not voters:
        return "No voters to reverse."
    percent = float(config.get("vote_to_remove.reversal_punishment_credit_tax_percent", 0.05) or 0.05)
    minimum = float(config.get("vote_to_remove.reversal_punishment_credit_minimum", 10.0) or 10.0)
    for voter_id in voters:
        voter = repo.get_user(voter_id)
        if not voter:
            continue
        penalty = -round_credits(max(voter.credits * percent, minimum))
        _, updated = repo.apply_credit_change(voter_id, penalty, "remove_reversal", daily_caps=None)
        maybe_apply_negative_cooldown(repo, config, updated)
        reply_to = _delivery_reply_for_user(store, message_id, voter_id)
        try:
            await bot.send_message(
                voter_id,
                f"Your remove vote was reversed by moderators. Penalty: {abs(penalty):.2f} credits. Balance: {updated.credits:.2f}.",
                reply_to_message_id=reply_to,
            )
        except TelegramError as exc:
            log_telegram_error(LOGGER, "moderation.revert_voter_notify", exc, aggregate=_aggregate(store), repo=repo, user_id=voter_id, message_id=message_id, reply_to=reply_to)
            pass
    if msg.sender_id:
        try:
            await bot.send_message(
                msg.sender_id,
                "Moderators did not confirm wrongdoing for your removed message.",
                reply_to_message_id=_sender_source_reply(msg),
            )
        except TelegramError as exc:
            log_telegram_error(LOGGER, "moderation.revert_sender_notify", exc, aggregate=_aggregate(store), repo=repo, user_id=msg.sender_id, message_id=message_id)
            pass
    msg.reverted = True
    _append_mod_action(msg, f"Reverted remove vote; punished {len(voters)} voter(s)")
    await send_mod_notes(bot, repo, store, config, message_id, reason="remove vote reverted", voter_ids=voters)
    return "Remove vote reverted."


async def remove_whisper(bot: Bot, repo: Repository, store: TransientStore, whisper_id: int, reason: str) -> int:
    whisper = store.whispers.get(whisper_id)
    if not whisper:
        return 0
    count = 0
    for delivery in store.deliveries_for_whisper(whisper_id):
        if delivery.deleted:
            continue
        user = repo.get_user(delivery.recipient_id)
        if user and user.is_mod_or_admin:
            text = f"<b>Removed whisper</b>\nReason: {html_escape(reason)}\n\n{whisper.text}"
            try:
                await bot.edit_message_text(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, text=text, parse_mode="HTML")
            except TelegramError as exc:
                log_telegram_error(LOGGER, "whisper.remove_mod_edit_html", exc, aggregate=_aggregate(store), repo=repo, user_id=delivery.recipient_id, whisper_id=whisper_id)
                try:
                    await bot.edit_message_text(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, text=_plain_text(text))
                except TelegramError as fallback_exc:
                    log_telegram_error(LOGGER, "whisper.remove_mod_edit_plain", fallback_exc, aggregate=_aggregate(store), repo=repo, user_id=delivery.recipient_id, whisper_id=whisper_id)
                    pass
            delivery.deleted = True
            count += 1
            continue
        try:
            await bot.edit_message_text(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, text="Whisper removed.")
        except TelegramError as exc:
            log_telegram_error(LOGGER, "whisper.remove_recipient_edit", exc, aggregate=_aggregate(store), repo=repo, user_id=delivery.recipient_id, whisper_id=whisper_id)
            try:
                await bot.delete_message(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id)
            except TelegramError as fallback_exc:
                log_telegram_error(LOGGER, "whisper.remove_recipient_delete", fallback_exc, aggregate=_aggregate(store), repo=repo, user_id=delivery.recipient_id, whisper_id=whisper_id)
                pass
        delivery.deleted = True
        count += 1
    whisper.deleted = True
    try:
        await bot.send_message(
            whisper.sender_id,
            f"Your whisper was removed: {html_escape(reason)}",
            parse_mode="HTML",
            reply_to_message_id=_whisper_reply_for_user(store, whisper_id, whisper.sender_id),
        )
    except TelegramError as exc:
        log_telegram_error(LOGGER, "whisper.remove_sender_notify", exc, aggregate=_aggregate(store), repo=repo, user_id=whisper.sender_id, whisper_id=whisper_id)
        pass
    return count


def _sender_source_reply(msg) -> int | None:
    return msg.source_message_id if msg.source_chat_id == msg.sender_id else None


def _delivery_reply_for_user(store: TransientStore, message_id: int, user_id: int) -> int | None:
    delivery = store.delivery_for_recipient(message_id, user_id)
    if delivery:
        return delivery.telegram_message_id
    for delivery in store.deliveries_for_message(message_id):
        if delivery.recipient_id == user_id:
            return delivery.tombstone_message_id or delivery.telegram_message_id
    return None


def _whisper_reply_for_user(store: TransientStore, whisper_id: int, user_id: int) -> int | None:
    delivery = next((d for d in store.deliveries_for_whisper(whisper_id) if d.recipient_id == user_id), None)
    return delivery.telegram_message_id if delivery else None


def _append_mod_action(msg, text: str) -> None:
    actions = msg.metadata.setdefault("mod_actions", [])
    actions.append(text)


def _aggregate(store: TransientStore):
    queue = getattr(store, "delivery_queue", None)
    return getattr(queue, "_aggregate_logger", None)
