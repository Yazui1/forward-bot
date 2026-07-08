from __future__ import annotations

import math
import logging
import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from forward_bot.cache.transient import TransientMessage
from forward_bot.commands.common import ensure_user, get_config, get_repo, get_store, touch_activity
from forward_bot.db.repository import User
from forward_bot.features.credits import apply_credit, downvote_cost, downvote_drop_seconds, maybe_apply_negative_cooldown
from forward_bot.features.media import MediaInspection, extract_payload
from forward_bot.features.onboarding import current_onboarding_question, onboarding_complete_message, onboarding_prompt, onboarding_questions, requires_onboarding_answers
from forward_bot.features.remove_votes import vote_to_remove
from forward_bot.features.tagging import TAG_BLOCKED, TAG_DUPLICATE, TAG_OK, TAG_POTENTIALLY_UNWANTED, TAG_QUESTIONABLE
from forward_bot.features.tombstones import mark_for_moderation_action, punish_sender, punish_whisper_sender, remove_for_moderators, remove_message, remove_whisper, revert_remove_vote, send_mod_notes, send_whisper_mod_notes
from forward_bot.identity import display_identity, display_identity_html
from forward_bot.logging_utils import log_telegram_error
from forward_bot.messages import MSG_BANNED, MSG_CACHE_MISS, MSG_RATE_LIMITED, MSG_USE_START
from forward_bot.utils import html_escape, human_seconds, iso, normalize_emoji, round_credits


UPVOTES = {"👍", "🔥", "❤", "❤️", "💛", "💚", "💙", "💜", "🧡", "🖤", "🤍", "🤎"}
DOWNVOTES = {"👎"}
LOGGER = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    user, _ = await ensure_user(update, context)
    if not user:
        return
    repo = get_repo(context)
    config = get_config(context)
    if user.is_banned:
        await _reply_to_message(context, msg, MSG_BANNED)
        return
    touch_activity(context, user.telegram_id)
    user = repo.get_user(user.telegram_id) or user
    if not user.has_started:
        await _reply_to_message(context, msg, MSG_USE_START)
        return
    if requires_onboarding_answers(user, repo):
        await _handle_onboarding_message(context, msg, user)
        return
    if user.active_cooldown_seconds > 0 and not user.is_mod_or_admin:
        _aggregate(context, "pipeline.cooldown_attempt")
        await _reply_to_message(context, msg, f"Cooldown active: {human_seconds(user.active_cooldown_seconds)}. Reason: {user.cooldown_reason or 'cooldown'}")
        await _broadcast_cooldown_attempt(update, context, user, extract_payload(msg), source_message=msg)
        return
    if msg.reply_to_message:
        if await _try_auto_whisper_reply(update, context, user):
            return
    allowed, retry = context.application.bot_data["rate_limiter"].check(
        user.telegram_id)
    if not allowed and not user.is_mod_or_admin:
        _aggregate(context, "pipeline.rate_limited")
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            f"Retry in {retry}s", callback_data=f"retry:{user.telegram_id}")]])
        await _reply_to_message(context, msg, MSG_RATE_LIMITED, reply_markup=markup)
        return
    await _process_payload(update, context, user, extract_payload(msg), source_message=msg)


async def submit_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    identity_mode: str | None = None,
    force_remove_buttons: bool = False,
) -> None:
    user, _ = await ensure_user(update, context)
    msg = update.effective_message
    if not user or not msg:
        return
    repo = get_repo(context)
    config = get_config(context)
    if user.is_banned:
        await _reply_to_message(context, msg, MSG_BANNED)
        return
    if not user.has_started:
        await _reply_to_message(context, msg, MSG_USE_START)
        return
    if requires_onboarding_answers(user, repo):
        await _reply_to_message(context, msg, onboarding_prompt(user, repo))
        return
    if user.active_cooldown_seconds > 0 and not user.is_mod_or_admin:
        _aggregate(context, "pipeline.cooldown_attempt")
        await _reply_to_message(context, msg, f"Cooldown active: {human_seconds(user.active_cooldown_seconds)}. Reason: {user.cooldown_reason or 'cooldown'}")
        payload = {"content_type": "text", "text": text, "media_file_id": None, "thumbnail_file_id": None, "media_kind": None, "mime_type": None,
                   "sticker_set_name": None, "is_animated": False, "is_video": False, "parse_mode": None, "force_remove_buttons": force_remove_buttons}
        await _broadcast_cooldown_attempt(update, context, user, payload, source_message=msg, identity_mode=identity_mode)
        return
    allowed, retry = context.application.bot_data["rate_limiter"].check(
        user.telegram_id)
    if not allowed and not user.is_mod_or_admin:
        _aggregate(context, "pipeline.rate_limited")
        await _reply_to_message(context, msg, MSG_RATE_LIMITED, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Retry in {retry}s", callback_data=f"retry:{user.telegram_id}")]]))
        return
    touch_activity(context, user.telegram_id)
    payload = {"content_type": "text", "text": text, "media_file_id": None, "thumbnail_file_id": None, "media_kind": None, "mime_type": None,
               "sticker_set_name": None, "is_animated": False, "is_video": False, "parse_mode": None, "force_remove_buttons": force_remove_buttons}
    await _process_payload(update, context, user, payload, source_message=msg, identity_mode=identity_mode)


async def _handle_onboarding_message(context: ContextTypes.DEFAULT_TYPE, msg: Message, user: User) -> None:
    repo = get_repo(context)
    question = current_onboarding_question(user, repo)
    if question is None:
        repo.set_onboarding_progress(user.telegram_id, acknowledged=True)
        return
    if msg.text != question.answer:
        await _reply_to_message(context, msg, onboarding_prompt(user, repo))
        return
    current_questions = onboarding_questions(repo)
    next_index = user.onboarding_question_index + 1
    complete = next_index >= len(current_questions)
    updated = repo.set_onboarding_progress(
        user.telegram_id,
        acknowledged=complete,
        question_index=next_index,
    ) or user
    if complete:
        await _reply_to_message(context, msg, onboarding_complete_message(updated))
    else:
        await _reply_to_message(context, msg, onboarding_prompt(updated, repo))


async def _broadcast_cooldown_attempt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    payload: dict[str, Any],
    *,
    source_message: Message,
    identity_mode: str | None = None,
) -> None:
    repo = get_repo(context)
    config = get_config(context)
    store = get_store(context)
    recipients = [u for u in repo.list_users(
    ) if u.has_started and u.is_mod_or_admin and not u.is_banned]
    for recipient in recipients:
        body, identity_parse_mode = _apply_identity(payload.get(
            "text") or "", user, identity_mode, payload.get("content_type", "text"))
        parse_mode = identity_parse_mode or payload.get("parse_mode")
        text = _cooldown_attempt_text(
            user, recipient, config, body, parse_mode)
        reply_target_id = _resolve_reply_target(source_message, user, context)
        tm = store.add_message(
            sender_id=user.telegram_id,
            content_type=payload.get("content_type", "text"),
            text=text,
            media_file_id=payload.get("media_file_id"),
            thumbnail_file_id=payload.get("thumbnail_file_id"),
            media_kind=payload.get("media_kind"),
            mime_type=payload.get("mime_type"),
            sticker_set_name=payload.get("sticker_set_name"),
            is_animated=bool(payload.get("is_animated")),
            is_video=bool(payload.get("is_video")),
            source_chat_id=source_message.chat_id,
            source_message_id=source_message.message_id,
            reply_to_message_id=None if reply_target_id == -1 else reply_target_id,
            parse_mode="HTML",
            is_system=False,
            urgent=True,
            metadata={
                "cooldown_attempt": True,
                "forward_from_chat_id": payload.get("forward_from_chat_id"),
                "forward_from_message_id": payload.get("forward_from_message_id"),
            },
        )
        context.application.bot_data["queue"].enqueue_message(tm, [recipient])


def _cooldown_attempt_text(user: User, viewer: User, config, body: str, parse_mode: str | None) -> str:
    header = (
        f"<i>In cooldown (Left: {html_escape(human_seconds(user.active_cooldown_seconds))}):</i> "
        f"{display_identity_html(user, config, viewer=viewer)}"
    )
    if not body:
        return header
    if parse_mode == "HTML":
        rendered_body = body
    else:
        rendered_body = html_escape(body)
    return f"{header}\n{rendered_body}"


async def _try_auto_whisper_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User) -> bool:
    msg = update.effective_message
    store = get_store(context)
    wdel = store.resolve_whisper_delivery(
        user.telegram_id, msg.reply_to_message.message_id)
    if not wdel:
        return False
    whisper = store.whispers.get(wdel.whisper_id)
    if not whisper or whisper.deleted:
        await _reply_to_message(context, msg, MSG_CACHE_MISS)
        return True
    text = msg.text or msg.caption or ""
    if not text.strip():
        await _reply_to_message(context, msg, "Whisper replies require text or caption.")
        return True
    target_id = whisper.sender_id if whisper.sender_id != user.telegram_id else whisper.target_id
    target = get_repo(context).get_user(target_id)
    if not target:
        await _reply_to_message(context, msg, "Recipient unavailable.")
        return True
    from forward_bot.commands.user_commands import _send_whisper
    await _send_whisper(update, context, user, target, text, reply_to_whisper_id=whisper.id, modwhisper=whisper.is_modwhisper)
    return True


async def _process_payload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    payload: dict[str, Any],
    *,
    source_message: Message,
    identity_mode: str | None = None,
) -> TransientMessage | None:
    text = payload.get("text") or ""
    payload["text"], identity_parse_mode = _apply_identity(
        text, user, identity_mode, payload.get("content_type", "text"))
    if identity_parse_mode:
        payload["parse_mode"] = identity_parse_mode
    reply_target_id = _resolve_reply_target(source_message, user, context)
    if reply_target_id == -1:
        await _reply_to_message(context, source_message, MSG_CACHE_MISS)
        return None
    mod_note_reply = _resolve_mod_note_reply(source_message, user, context)
    store = get_store(context)
    tm = store.add_message(
        sender_id=user.telegram_id,
        content_type=payload.get("content_type", "text"),
        text=payload.get("text") or "",
        media_file_id=payload.get("media_file_id"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        media_kind=payload.get("media_kind"),
        mime_type=payload.get("mime_type"),
        sticker_set_name=payload.get("sticker_set_name"),
        is_animated=bool(payload.get("is_animated")),
        is_video=bool(payload.get("is_video")),
        source_chat_id=source_message.chat_id,
        source_message_id=source_message.message_id,
        reply_to_message_id=reply_target_id,
        parse_mode=payload.get("parse_mode"),
        metadata={
            "forward_from_chat_id": payload.get("forward_from_chat_id"),
            "forward_from_message_id": payload.get("forward_from_message_id"),
            "mod_only": bool(mod_note_reply),
            "reply_to_mod_note": bool(mod_note_reply),
        },
    )
    if mod_note_reply:
        tm.reply_to_message_id = mod_note_reply
    media_service = context.application.bot_data["media"]
    inspection = await media_service.inspect(context.bot, tm.id, payload)
    tagger = context.application.bot_data["tagger"]
    result = await tagger.classify(payload, inspection)
    tm.tag = result.tag
    tm.tag_reason = result.reason
    tm.media_hash = result.media_hash
    tm.media_hash_first_seen_at = result.media_hash_first_seen_at
    tm.remove_buttons = bool(
        result.remove_buttons or payload.get("force_remove_buttons"))
    store.set_sender_snapshot(tm.id, _sender_snapshot(
        user, inspection, result.reason))
    if result.tag == TAG_BLOCKED:
        _aggregate(context, "pipeline.blocked")
        await _reply_to_message(context, source_message, f"Message blocked: {result.reason or 'blocked'}")
        if result.reason == "blocked-sticker-set":
            await _deliver_to_mods(context, tm, notice=f"Sticker blocked: {payload.get('sticker_set_name')}")
        else:
            media_service.release(tm.id)
        return None
    if result.tag == TAG_DUPLICATE:
        _aggregate(context, "pipeline.duplicate")
        await _reply_to_message(context, source_message, "This was sent recently. Please wait before sending again or send something new.")
        media_service.release(tm.id)
        return None
    if result.tag == TAG_QUESTIONABLE and user.confirmation_enabled:
        _aggregate(context, "pipeline.questionable")
        store.add_confirmation(tm.id, int(get_config(context).get(
            "cache.pending_state_ttl_seconds", 86400) or 86400))
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Send anyway", callback_data=f"confirm:{tm.id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{tm.id}"),
        ]])
        await _reply_to_message(context, source_message, f"Questionable message: {result.reason or 'review needed'}", reply_markup=markup)
        return tm
    await distribute_message(context, tm)
    return tm


def _apply_identity(text: str, user: User, identity_mode: str | None, content_type: str = "text") -> tuple[str, str | None]:
    if identity_mode == "signed" or (identity_mode is None and user.sign_enabled):
        identity = f"@{user.username}" if user.username else "signed"
        suffix = f"~ {identity}" if text else identity
        return (f"{html_escape(text)} <i>{html_escape(suffix)}</i>" if text else f"<i>{html_escape(suffix)}</i>"), "HTML"
    if (identity_mode == "tripcode" or (identity_mode is None and user.tripcode_enabled)) and user.tripcode_name and user.tripcode_hash:
        trip = f"<b>{html_escape(user.tripcode_name)}</b> !{html_escape(user.tripcode_hash)}"
        return (f"{trip}:\n{html_escape(text)}" if text else trip), "HTML"
    return text, None


def _resolve_reply_target(source_message: Message, user: User, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not source_message.reply_to_message:
        return None
    store = get_store(context)
    delivery = store.resolve_delivery(
        user.telegram_id, source_message.reply_to_message.message_id)
    if delivery:
        return delivery.message_id
    own = store.resolve_source(
        source_message.chat_id, source_message.reply_to_message.message_id)
    if own:
        return own.id
    if store.resolve_whisper_delivery(user.telegram_id, source_message.reply_to_message.message_id):
        return None
    mod_note_msg = store.resolve_mod_note(
        user.telegram_id, source_message.reply_to_message.message_id)
    if mod_note_msg:
        return mod_note_msg.id
    return -1


def _resolve_mod_note_reply(source_message: Message, user: User, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not user.is_mod_or_admin or not source_message.reply_to_message:
        return None
    msg = get_store(context).resolve_mod_note(user.telegram_id,
                                              source_message.reply_to_message.message_id)
    return msg.id if msg else None


def _sender_snapshot(user: User, inspection, reason: str | None) -> dict[str, Any]:
    return {
        "user_id": user.telegram_id,
        "username": user.username,
        "credits": user.credits,
        "warnings": user.warning_count,
        "media_width": inspection.width,
        "media_height": inspection.height,
        "media_bytes": inspection.byte_size,
        "tag_reason": reason,
    }


async def distribute_message(context: ContextTypes.DEFAULT_TYPE, tm: TransientMessage) -> None:
    repo = context.application.bot_data["repo"]
    config = get_config(context)
    sender = repo.get_user(tm.sender_id) if tm.sender_id else None
    if tm.metadata.get("mod_only"):
        recipients = [u for u in repo.list_users(
        ) if u.has_started and u.is_mod_or_admin and not u.is_banned and u.telegram_id != tm.sender_id]
    else:
        recipients = repo.eligible_recipients(tm.sender_id)
    if tm.tag == TAG_POTENTIALLY_UNWANTED:
        recipients = [
            u for u in recipients if u.is_mod_or_admin or not u.hide_potentially_unwanted]
    if tm.tag == TAG_QUESTIONABLE:
        tm.remove_buttons = True
    queued = context.application.bot_data["queue"].enqueue_message(
        tm, recipients)
    if not queued:
        return
    if sender and not tm.is_system:
        reason = "text_message_reward" if tm.content_type == "text" else "media_message_reward"
        reward = float(config.get(f"credits.{reason}", 0) or 0)
        apply_credit(repo, config, sender.telegram_id, reward, reason)
        touch_activity(context, sender.telegram_id)


async def _deliver_to_mods(context: ContextTypes.DEFAULT_TYPE, tm: TransientMessage, notice: str) -> None:
    repo = get_repo(context)
    mods = [u for u in repo.list_users(
    ) if u.has_started and u.is_mod_or_admin and not u.is_banned]
    queued = context.application.bot_data["queue"].enqueue_message(tm, mods)
    notice_msg = get_store(context).add_message(
        sender_id=tm.sender_id, content_type="text", text=notice, is_system=True, urgent=True)
    context.application.bot_data["queue"].enqueue_message(notice_msg, mods)
    if not queued:
        context.application.bot_data["media"].release(tm.id)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg or not update.effective_user:
        return
    repo = get_repo(context)
    config = get_config(context)
    user, _ = await ensure_user(update, context)
    tm = get_store(context).resolve_source(msg.chat_id, msg.message_id)
    if not tm:
        await _reply_to_message(context, msg, MSG_CACHE_MISS)
        return
    if not user or tm.sender_id != user.telegram_id:
        await _reply_to_message(context, msg, "You can edit only your own messages.")
        return
    cost = float(config.get("credits.edit_cost", 5) or 5)
    if user.credits < cost and not user.is_admin:
        await _reply_to_message(context, msg, "Insufficient credits.")
        return
    payload = extract_payload(msg)
    if payload.get("content_type") != tm.content_type:
        await _reply_to_message(context, msg, "Edited content type must match original.")
        return
    payload["text"], identity_parse_mode = _apply_identity(payload.get(
        "text") or "", user, None, payload.get("content_type", "text"))
    if identity_parse_mode:
        payload["parse_mode"] = identity_parse_mode
    result = await context.application.bot_data["tagger"].classify(
        {**payload, "content_type": "text",
            "media_file_id": None, "thumbnail_file_id": None},
        MediaInspection(),
    )
    if result.tag != TAG_OK:
        await _reply_to_message(context, msg, f"Edit rejected: {result.reason or result.tag}")
        return
    tm.text = payload.get("text") or ""
    tm.parse_mode = payload.get("parse_mode")
    updated = 0
    for delivery in get_store(context).deliveries_for_message(tm.id):
        try:
            if tm.content_type == "text":
                await context.bot.edit_message_text(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, text=tm.text, parse_mode=tm.parse_mode)
                updated += 1
            elif tm.content_type in {"photo", "video", "animation", "document"}:
                await context.bot.edit_message_caption(chat_id=delivery.recipient_id, message_id=delivery.telegram_message_id, caption=tm.text or None, parse_mode=tm.parse_mode)
                updated += 1
        except TelegramError as exc:
            log_telegram_error(LOGGER, "handler.edit_delivered_copy", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), recipient_id=delivery.recipient_id, telegram_message_id=delivery.telegram_message_id)
            pass
    if not user.is_admin:
        apply_credit(repo, config, user.telegram_id, -
                     cost, "edit_cost", cap_positive=False)
    touch_activity(context, user.telegram_id)
    updated_user = repo.get_user(user.telegram_id)
    await _reply_to_message(context, msg, f"Edited {updated} copies. Cost: {cost:.2f}. Balance: {updated_user.credits:.2f}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    user, _ = await ensure_user(update, context)
    if data.startswith("retry:"):
        if not user:
            await query.answer("User not found.", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        if user.telegram_id != target_id:
            await query.answer("This retry button is not yours.", show_alert=True)
            return
        remaining = context.application.bot_data["rate_limiter"].remaining_seconds(
            user.telegram_id)
        label = f"Retry in {remaining}s" if remaining else "You can send again"
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"retry:{user.telegram_id}")]]))
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log_telegram_error(LOGGER, "callback.retry_markup", exc, aggregate=context.application.bot_data.get(
                    "aggregate_logger"), user_id=user.telegram_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "callback.retry_markup", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), user_id=user.telegram_id)
        await query.answer(label)
        return
    if data.startswith("confirm:"):
        message_id = int(data.split(":", 1)[1])
        tm = get_store(context).get_message(message_id)
        if not tm or not get_store(context).consume_confirmation(message_id):
            context.application.bot_data["media"].release(message_id)
            await query.edit_message_text("Confirmation expired.")
            await query.answer("Confirmation expired.", show_alert=True)
            return
        tm.remove_buttons = True
        await distribute_message(context, tm)
        await query.edit_message_text("Sent.")
        await query.answer("Sent.")
        return
    if data.startswith("cancel:"):
        message_id = int(data.split(":", 1)[1])
        get_store(context).confirmations.pop(message_id, None)
        context.application.bot_data["media"].release(message_id)
        await query.edit_message_text("Cancelled.")
        await query.answer("Cancelled.")
        return
    if data.startswith("rm:"):
        if not user:
            await query.answer("User not found.", show_alert=True)
            return
        message_id = int(data.split(":", 1)[1])
        ok, text = await vote_to_remove(context.bot, get_repo(context), get_store(context), get_config(context), message_id, user)
        if ok or "already voted" in text.lower():
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError as exc:
                log_telegram_error(LOGGER, "callback.remove_button_clear", exc, aggregate=context.application.bot_data.get(
                    "aggregate_logger"), message_id=message_id)
                pass
        elif "cooldown:" in text.lower():
            try:
                left = text.split(":", 1)[1].strip().rstrip(".")
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        f"Vote remove ({left})", callback_data=f"rm:{message_id}")]])
                )
            except Exception:
                pass
        await query.answer(text, show_alert=not ok)
        return
    if data.startswith("mconf:"):
        if not user or not user.is_mod_or_admin:
            await query.answer("Not allowed.", show_alert=True)
            return
        message_id = int(data.split(":")[1])
        if message_id < 0:
            text = await punish_whisper_sender(context.bot, get_repo(context), get_store(context), get_config(context), abs(message_id), user.telegram_id)
            await query.answer(text, show_alert=True)
            return
        text = await punish_sender(context.bot, get_repo(context), get_store(context), get_config(context), message_id, user.telegram_id)
        await query.answer(text, show_alert=True)
        return
    if data.startswith("mrm:"):
        if not user or not user.is_mod_or_admin:
            await query.answer("Not allowed.", show_alert=True)
            return
        message_id = int(data.split(":")[1])
        if message_id < 0:
            reason = "removed for moderators"
            count = await remove_whisper(context.bot, get_repo(context), get_store(context), get_config(context), abs(message_id), reason, remove_for_mods=True, send_note=False, notify_sender=False)
            await send_whisper_mod_notes(context.bot, get_repo(context), get_store(context), get_config(context), abs(message_id), reason=reason, removed_for_mods=True)
            await query.answer(f"Removed {count} moderator copies.", show_alert=True)
            return
        text = await remove_for_moderators(context.bot, get_repo(context), get_store(context), get_config(context), message_id, user.telegram_id)
        await query.answer(text, show_alert=True)
        return
    if data.startswith("mrev:"):
        if not user or not user.is_mod_or_admin:
            await query.answer("Not allowed.", show_alert=True)
            return
        message_id = int(data.split(":")[1])
        text = await revert_remove_vote(context.bot, get_repo(context), get_store(context), get_config(context), message_id)
        await query.answer(text, show_alert=True)
        return
    if data.startswith("mban:"):
        if not user or not user.is_admin:
            await query.answer("Admin only.", show_alert=True)
            return
        _, message_id_s, sender_id_s = data.split(":", 2)
        message_id = int(message_id_s)
        repo = get_repo(context)
        store = get_store(context)
        config = get_config(context)
        source = store.get_message(message_id) if message_id > 0 else None
        whisper = store.whispers.get(
            abs(message_id)) if message_id < 0 else None
        sender_id = int(sender_id_s) or (whisper.sender_id if whisper else 0)
        if not sender_id:
            await query.answer("Sender not found.", show_alert=True)
            return
        banned_sender = repo.set_role(sender_id, banned=True)
        purged = 0
        for cached in list(store.messages.values()):
            if cached.sender_id != sender_id:
                continue
            touched = False
            if not cached.deleted:
                await remove_message(context.bot, repo, store, config, cached.id, reason="banned", remove_for_mods=False, notify_sender=False, send_note=False)
                touched = True
            if not cached.removed_for_mods:
                await remove_message(context.bot, repo, store, config, cached.id, reason="banned", remove_for_mods=True, notify_sender=False, send_note=False)
                touched = True
            if touched:
                purged += 1
        if source:
            source.metadata["ban_purged_count"] = purged
            await send_mod_notes(
                context.bot,
                repo,
                store,
                config,
                source.id,
                reason="banned",
                voter_ids=[],
            )
        elif whisper:
            await send_whisper_mod_notes(
                context.bot,
                repo,
                store,
                config,
                whisper.id,
                reason="banned",
            )
        try:
            await context.bot.send_message(sender_id, "You are banned.", reply_to_message_id=source.source_message_id if source else store.deliveries_for_whisper(whisper.id)[0].telegram_message_id if whisper else None)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "callback.ban_notify", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), repo=get_repo(context), user_id=sender_id)
            pass
        await query.answer(f"Banned {display_identity(banned_sender, config, viewer=user)}. Purged {purged} cached messages.", show_alert=True)
        return
    if data.startswith("facc:") or data.startswith("fdec:"):
        await _fight_callback(update, context, accept=data.startswith("facc:"))
        return
    await query.answer()


async def _fight_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, *, accept: bool) -> None:
    query = update.callback_query
    user, _ = await ensure_user(update, context)
    if not user or not query:
        return
    fight_id = int(query.data.split(":")[1])
    store = get_store(context)
    repo = get_repo(context)
    config = get_config(context)
    fight = store.get_fight(fight_id)
    if not fight or fight.status != "pending":
        await query.edit_message_text("Fight expired or resolved.")
        await query.answer("Fight expired or resolved.", show_alert=True)
        return
    if user.telegram_id != fight.target_id:
        await query.answer("Only the target can answer.", show_alert=True)
        return
    if not accept:
        fight.status = "declined"
        touch_activity(context, user.telegram_id)
        try:
            await context.bot.send_message(fight.sender_id, "Fight declined.", reply_to_message_id=fight.command_message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "fight.decline_notify", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), repo=repo, user_id=fight.sender_id)
            pass
        await query.edit_message_text("Fight declined.")
        await query.answer("Fight declined.")
        return
    sender = repo.get_user(fight.sender_id)
    target = repo.get_user(fight.target_id)
    if not sender or not target or sender.credits < fight.stake or target.credits < fight.stake:
        fight.status = "expired"
        try:
            await context.bot.send_message(fight.sender_id, "Fight expired: insufficient credits.", reply_to_message_id=fight.command_message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "fight.expired_notify", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), repo=repo, user_id=fight.sender_id)
            pass
        await query.edit_message_text("Fight expired: insufficient credits.")
        await query.answer("Fight expired: insufficient credits.", show_alert=True)
        return
    tier_diff = math.floor(math.log2(max(1.0, sender.credits))) - \
        math.floor(math.log2(max(1.0, target.credits)))
    p_sender = max(0.1, min(0.9, 0.5 + 0.1 * tier_diff))
    sender_wins = random.random() < p_sender
    winner = sender if sender_wins else target
    loser = target if sender_wins else sender
    tax = float(config.get("fights.win_tax_percent", 0.05) or 0.05)
    win_amount = round_credits(fight.stake * (1 - tax))
    winner_delta, _ = apply_credit(
        repo, config, winner.telegram_id, win_amount, "fight_win")
    loser_delta, loser_after = apply_credit(
        repo, config, loser.telegram_id, -fight.stake, "fight_loss", cap_positive=False)
    maybe_apply_negative_cooldown(repo, config, loser_after)
    touch_activity(context, sender.telegram_id)
    touch_activity(context, target.telegram_id)
    fight.status = "completed"
    for participant in (sender, target):
        refreshed = repo.get_user(participant.telegram_id)
        won = participant.telegram_id == winner.telegram_id
        outcome = "won" if won else "lost"
        delta = winner_delta if won else loser_delta
        reply_to = fight.command_message_id if participant.telegram_id == sender.telegram_id else getattr(
            query.message, "message_id", None)
        try:
            await context.bot.send_message(
                participant.telegram_id,
                f"You {outcome} the fight. Delta: {delta:+.2f}. Stake: {fight.stake:.2f}. Matchup: {fight.matchup}. Balance: {refreshed.credits:.2f}.",
                reply_to_message_id=reply_to,
            )
        except TelegramError as exc:
            log_telegram_error(LOGGER, "fight.result_notify", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), repo=repo, user_id=participant.telegram_id, reply_to=reply_to)
            pass
    await query.edit_message_text("Fight resolved.")
    await query.answer("Fight resolved.")


async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction = update.message_reaction
    if not reaction or not reaction.user or not reaction.chat:
        return
    user, _ = await ensure_user(update, context)
    if not user:
        return
    emojis = {normalize_emoji(getattr(r, "emoji", str(r)))
              for r in (reaction.new_reaction or [])}
    if not emojis:
        return
    config = get_config(context)
    repo = get_repo(context)
    store = get_store(context)
    delivery = store.resolve_delivery(user.telegram_id, reaction.message_id)
    wdel = store.resolve_whisper_delivery(
        user.telegram_id, reaction.message_id)
    delete_emoji = normalize_emoji(
        str(config.get("moderation.delete_reaction_emoji", "✍️")))
    if delete_emoji in emojis:
        if user.is_mod_or_admin:
            if delivery:
                reason = "deleted by moderator"
                subject = store.get_message(delivery.message_id)
                if subject:
                    subject.metadata.setdefault(
                        "mod_actions", []).append(reason)
                await mark_for_moderation_action(context.bot, repo, store, config, delivery.message_id)
                await remove_message(context.bot, repo, store, config, delivery.message_id, reason=reason, remove_for_mods=False)
            elif wdel:
                whisper = store.whispers.get(wdel.whisper_id)
                if not whisper or whisper.deleted:
                    try:
                        await context.bot.send_message(user.telegram_id, MSG_CACHE_MISS, reply_to_message_id=reaction.message_id)
                    except TelegramError as exc:
                        log_telegram_error(LOGGER, "reaction.cache_miss", exc, aggregate=context.application.bot_data.get(
                            "aggregate_logger"), repo=repo, user_id=user.telegram_id, reply_to=reaction.message_id)
                        pass
                else:
                    whisper_id = _merge_related_modwhispers(store, whisper)
                    await remove_whisper(context.bot, repo, store, config, whisper_id, "deleted by moderator")
            else:
                try:
                    await context.bot.send_message(user.telegram_id, MSG_CACHE_MISS, reply_to_message_id=reaction.message_id)
                except TelegramError as exc:
                    log_telegram_error(LOGGER, "reaction.cache_miss", exc, aggregate=context.application.bot_data.get(
                        "aggregate_logger"), repo=repo, user_id=user.telegram_id, reply_to=reaction.message_id)
                    pass
            return
        if delivery:
            _, text = await vote_to_remove(context.bot, repo, store, config, delivery.message_id, user)
            try:
                await context.bot.send_message(user.telegram_id, text, reply_to_message_id=reaction.message_id)
            except TelegramError as exc:
                log_telegram_error(LOGGER, "reaction.deletevote_notify", exc, aggregate=context.application.bot_data.get(
                    "aggregate_logger"), repo=repo, user_id=user.telegram_id, reply_to=reaction.message_id)
            return
    up = bool(emojis & UPVOTES)
    down = bool(emojis & DOWNVOTES)
    if not up and not down:
        return
    if delivery:
        subject = store.get_message(delivery.message_id)
        if not subject or not subject.sender_id:
            await context.bot.send_message(user.telegram_id, MSG_CACHE_MISS, reply_to_message_id=reaction.message_id)
            return
        await _apply_vote(context, user, subject.sender_id, f"msg:{subject.id}", up=up and not down, voter_reply_to=reaction.message_id)
    elif wdel:
        whisper = store.whispers.get(wdel.whisper_id)
        if whisper:
            await _apply_vote(context, user, whisper.sender_id, f"whisper:{whisper.id}", up=up and not down, voter_reply_to=reaction.message_id)
    else:
        try:
            await context.bot.send_message(user.telegram_id, MSG_CACHE_MISS, reply_to_message_id=reaction.message_id)
        except TelegramError as exc:
            log_telegram_error(LOGGER, "reaction.cache_miss", exc, aggregate=context.application.bot_data.get(
                "aggregate_logger"), repo=repo, user_id=user.telegram_id, reply_to=reaction.message_id)
            pass


def _merge_related_modwhispers(store, whisper) -> int:
    if not whisper.is_modwhisper:
        return whisper.id
    related = [
        other for other in store.whispers.values()
        if other.id != whisper.id
        and other.is_modwhisper
        and not other.deleted
        and other.sender_id == whisper.sender_id
        and other.text == whisper.text
        and abs((other.created_at - whisper.created_at).total_seconds()) <= 30
    ]
    if not related:
        return whisper.id
    target_ids = store.whisper_delivery_by_whisper_index.setdefault(
        whisper.id, set())
    for other in related:
        for delivery in store.deliveries_for_whisper(other.id):
            delivery.whisper_id = whisper.id
            target_ids.add(delivery.id)
        store.whisper_delivery_by_whisper_index.pop(other.id, None)
        other.deleted = True
    return whisper.id


async def _apply_vote(context: ContextTypes.DEFAULT_TYPE, voter: User, sender_id: int, key: str, *, up: bool, voter_reply_to: int | None = None) -> None:
    repo = get_repo(context)
    config = get_config(context)
    store = get_store(context)
    if sender_id == voter.telegram_id:
        await _dm(context, voter.telegram_id, "You cannot vote on your own message.", reply_to_message_id=voter_reply_to)
        return
    if voter.active_cooldown_seconds > 0:
        await _dm(context, voter.telegram_id, f"You cannot vote while in cooldown: {human_seconds(voter.active_cooldown_seconds)}.", reply_to_message_id=voter_reply_to)
        return
    scope, subject = key.split(":", 1)
    sender = repo.get_user(sender_id)
    if not sender:
        await _dm(context, voter.telegram_id, MSG_CACHE_MISS, reply_to_message_id=voter_reply_to)
        return
    if up:
        cost = float(config.get("credits.upvote_cost", 0.5) or 0.5)
        if voter.credits < cost:
            await _dm(context, voter.telegram_id, "Insufficient credits for upvote.", reply_to_message_id=voter_reply_to)
            return
        if not store.add_vote(scope, int(subject), voter.telegram_id):
            await _dm(context, voter.telegram_id, "You already voted on this.", reply_to_message_id=voter_reply_to)
            return
        apply_credit(repo, config, voter.telegram_id, -
                     cost, "upvote_cost", cap_positive=False)
        reward, sender_after = apply_credit(repo, config, sender_id, float(
            config.get("credits.upvote_reward", 1) or 1), "upvote_reward")
        repo.increment_vote_stat(sender_id, up=True)
        sender_reply_to = _sender_vote_reply_to(
            store, sender_id, scope, int(subject))
        if sender_after and sender_after.votes_enabled:
            await _dm(context, sender_id, f"Upvote received: +{reward:.2f} credits. Balance: {sender_after.credits:.2f}.", reply_to_message_id=sender_reply_to)
        voter_after = repo.get_user(voter.telegram_id)
        _aggregate(context, "vote.upvote")
        await _dm(context, voter.telegram_id, f"Upvote sent. Cost: {cost:.2f}. Balance: {voter_after.credits:.2f}", reply_to_message_id=voter_reply_to)
    else:
        streak, last = repo.get_downvote_state(voter.telegram_id)
        cost, next_streak, _ = downvote_cost(config, streak, last)
        if voter.credits < cost:
            await _dm(context, voter.telegram_id, "Insufficient credits for downvote.", reply_to_message_id=voter_reply_to)
            return
        if not store.add_vote(scope, int(subject), voter.telegram_id):
            await _dm(context, voter.telegram_id, "You already voted on this.", reply_to_message_id=voter_reply_to)
            return
        apply_credit(repo, config, voter.telegram_id, -cost,
                     "downvote_cost", cap_positive=False)
        vote_time = iso()
        repo.set_downvote_state(voter.telegram_id, next_streak, vote_time)
        penalty, sender_after = apply_credit(repo, config, sender_id, -float(config.get(
            "credits.downvote_penalty", 1) or 1), "downvote_penalty", cap_positive=False)
        maybe_apply_negative_cooldown(repo, config, sender_after)
        repo.increment_vote_stat(sender_id, up=False)
        sender_reply_to = _sender_vote_reply_to(
            store, sender_id, scope, int(subject))
        if sender_after and sender_after.votes_enabled:
            await _dm(context, sender_id, f"Downvote received: {penalty:.2f} credits. Balance: {sender_after.credits:.2f}.", reply_to_message_id=sender_reply_to)
        voter_after = repo.get_user(voter.telegram_id)
        next_cost, _, _ = downvote_cost(config, next_streak, vote_time)
        drop = downvote_drop_seconds(config, next_streak, vote_time)
        _aggregate(context, "vote.downvote")
        await _dm(context, voter.telegram_id, f"Downvote sent. Cost: {cost:.2f}. Balance: {voter_after.credits:.2f}. Next cost: {next_cost:.2f}. Drop in: {human_seconds(drop)}", reply_to_message_id=voter_reply_to)


def _sender_vote_reply_to(store, sender_id: int, scope: str, subject_id: int) -> int | None:
    if scope == "msg":
        msg = store.get_message(subject_id)
        if msg and msg.sender_id == sender_id and msg.source_chat_id == sender_id:
            return msg.source_message_id
        delivery = store.delivery_for_recipient(subject_id, sender_id)
        return delivery.telegram_message_id if delivery else None
    if scope == "whisper":
        delivery = next((d for d in store.deliveries_for_whisper(
            subject_id) if d.recipient_id == sender_id), None)
        return delivery.telegram_message_id if delivery else None
    return None


async def _dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, *, reply_to_message_id: int | None = None) -> None:
    try:
        await context.bot.send_message(user_id, text, reply_to_message_id=reply_to_message_id)
    except TelegramError as exc:
        log_telegram_error(LOGGER, "handler.dm", exc, aggregate=context.application.bot_data.get(
            "aggregate_logger"), repo=get_repo(context), user_id=user_id, reply_to=reply_to_message_id)
        pass


async def _reply_to_message(context: ContextTypes.DEFAULT_TYPE, msg: Message, text: str, **kwargs: Any) -> None:
    try:
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=text,
            reply_to_message_id=msg.message_id,
            **kwargs,
        )
    except TelegramError as exc:
        log_telegram_error(LOGGER, "handler.reply_to_message", exc, aggregate=context.application.bot_data.get(
            "aggregate_logger"), repo=get_repo(context), user_id=msg.chat_id, chat_id=msg.chat_id, message_id=msg.message_id)
        pass


def _aggregate(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    aggregate = context.application.bot_data.get("aggregate_logger")
    if aggregate:
        aggregate.increment(key)
