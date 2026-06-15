from __future__ import annotations

import secrets
import html
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
import math
import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, MessageReactionHandler, filters

from forward_bot.cache.state import CachedSenderMetadata, EphemeralState, SenderMetadataCache
from forward_bot.crypto.obfuscation import temporal_id
from forward_bot.features.credits import (
    adjust_credits_with_daily_limit,
    apply_negative_credit_cooldown,
    interpolate_downvote_cost,
)
from forward_bot.features.tagging import TaggingPipeline
from forward_bot.features.tombstones import append_action_info_to_message_for_mods, refresh_moderation_notes, remove_message_for_mods, remove_message_with_tombstones
from forward_bot.features.remove_votes import check_remove_vote_allowed
from forward_bot.messages import Messages as Msg
from forward_bot.utils import as_utc

logger = logging.getLogger(__name__)


def register_message_handlers(
    app: Any,
    repo: Any,
    cfg: dict[str, Any],
    rate_limiter: Any,
    queue: Any,
    tagger: TaggingPipeline,
    state: EphemeralState,
    sender_cache: SenderMetadataCache,
) -> None:
    app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE, _edited_message(repo, cfg, tagger)))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, _incoming(
        repo, cfg, rate_limiter, queue, tagger, state, sender_cache)))
    app.add_handler(CallbackQueryHandler(_retry(
        repo, cfg, rate_limiter, queue, tagger, state, sender_cache), pattern=r"^retry:"))
    app.add_handler(CallbackQueryHandler(
        _confirm(repo, cfg, queue, state), pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(_cancel(state), pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(
        _delete_vote_callback(repo, cfg), pattern=r"^rm:"))
    app.add_handler(CallbackQueryHandler(
        _mod_confirm(repo, cfg), pattern=r"^mconf:"))
    app.add_handler(CallbackQueryHandler(
        _mod_remove_for_mods(repo), pattern=r"^mrm:"))
    app.add_handler(CallbackQueryHandler(
        _mod_revert(repo, cfg), pattern=r"^mrev:"))
    app.add_handler(CallbackQueryHandler(
        _delete_confirm(repo, cfg), pattern=r"^delconf:"))
    app.add_handler(CallbackQueryHandler(
        _delete_cancel(repo), pattern=r"^delcancel:"))
    app.add_handler(CallbackQueryHandler(
        _fight_accept(repo, cfg), pattern=r"^facc:"))
    app.add_handler(CallbackQueryHandler(
        _fight_decline(repo), pattern=r"^fdec:"))
    app.add_handler(MessageReactionHandler(_reaction_vote(repo, cfg)))


def _incoming(repo: Any, cfg: dict[str, Any], rate_limiter: Any, queue: Any, tagger: TaggingPipeline, state: EphemeralState, sender_cache: SenderMetadataCache):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        user = update.effective_user
        if msg is None or user is None:
            return

        logger.debug("Incoming message user_id=%s message_id=%s",
                     user.id, msg.message_id)
        db_user = await repo.get_or_create_user(
            user.id,
            user.username,
            set(int(x) for x in cfg["bot"].get("admin_ids", [])),
            starting_credits=float(cfg["credits"]["starting_balance"]),
        )
        if db_user.is_banned:
            await msg.reply_text(Msg.BANNED)
            return
        await repo.touch_activity(user.id)
        db_user = await repo.get_user(user.id) or db_user
        cd = await repo.get_active_cooldown(user.id)
        if cd is not None and not (db_user.is_moderator or db_user.is_admin):
            await msg.reply_text(
                Msg.cooldown_remaining_with_reason(
                    _cooldown_remaining_text(cd),
                    str(cd["reason"] or "cooldown"),
                )
            )
            await _send_cooldown_message_to_mods(
                msg,
                user.id,
                user.username,
                cd,
                repo,
                cfg,
                queue,
                sender_cache,
            )
            return
        if not db_user.has_started:
            await msg.reply_text(Msg.USE_START_FIRST)
            return

        if msg.reply_to_message:
            whisper = await repo.whisper_context_by_reply(user.id, msg.reply_to_message.message_id)
            if whisper is not None:
                text = (msg.text or msg.caption or "").strip()
                if not text:
                    await msg.reply_text(Msg.WHISPER_TEXT_REQUIRED)
                    return
                try:
                    cost, balance = await _send_auto_whisper_reply(
                        context,
                        repo,
                        cfg,
                        db_user,
                        whisper,
                        text,
                        msg.message_id,
                    )
                except RuntimeError as exc:
                    await msg.reply_text(str(exc) or Msg.WHISPER_INSUFFICIENT_CREDITS)
                    return
                await msg.reply_text(Msg.whisper_sent(cost, balance))
                return

        allowed, retry_in = rate_limiter.check(user.id)
        if not allowed:
            token = secrets.token_urlsafe(8)
            payload = _extract_payload(msg)
            state.set_retry(token, payload | {
                            "sender_id": user.id, "username": user.username})
            system = await msg.reply_text(
                Msg.RATE_LIMIT_REPLY.format(seconds=retry_in),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        Msg.RETRY_BUTTON, callback_data=f"retry:{token}")]]
                ),
                reply_to_message_id=msg.message_id,
            )
            state.set_retry(f"sys:{token}", {
                            "chat_id": system.chat_id, "message_id": system.message_id})
            logger.debug("Rate limit hit user_id=%s retry_in=%s",
                         user.id, retry_in)
            return

        await _run_pipeline(msg, user.id, user.username, repo, cfg, queue, tagger, state, sender_cache)

    return handler


def _edited_message(repo: Any, cfg: dict[str, Any], tagger: TaggingPipeline):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.edited_message
        user = update.effective_user
        if msg is None or user is None:
            return
        source = await repo.message_by_source(msg.chat_id, msg.message_id)
        if source is None:
            await msg.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        if int(source["sender_id"]) != user.id:
            await msg.reply_text(Msg.EDIT_OWN_ONLY)
            return
        db_user = await repo.get_user(user.id)
        if db_user is None:
            return
        cost = float(cfg["credits"].get("edit_cost", 2.0))
        if db_user.credits < cost:
            await msg.reply_text(Msg.edit_insufficient(cost, db_user.credits), reply_to_message_id=msg.message_id)
            return

        payload = _extract_payload(msg)
        if payload.get("content_type") != source.get("content_type"):
            await msg.reply_text(Msg.EDIT_SAME_TYPE_ONLY, reply_to_message_id=msg.message_id)
            return
        payload = _apply_sender_identity(payload, db_user, user.username)
        tag = await tagger.run_once(
            payload.get("text"),
            None,
            None,
            None,
            repo=repo,
            cfg=cfg,
            message_id=int(source["id"]),
        )
        if tag.tag in {"BLOCKED", "QUESTIONABLE", "POTENTIALLY_UNWANTED"}:
            await msg.reply_text(Msg.EDIT_REJECTED, reply_to_message_id=msg.message_id)
            return

        message_id = int(source["id"])
        await repo.update_message_text_content(message_id, payload.get("text"), payload.get("parse_mode"))
        edited = await _edit_delivered_messages(
            context,
            repo,
            message_id,
            str(source["content_type"]),
            payload.get("text"),
            payload.get("parse_mode"),
        )
        balance = await repo.adjust_credits(user.id, -cost, "edit_cost")
        await repo.touch_activity(user.id)
        await msg.reply_text(
            Msg.edit_applied(cost, balance, edited),
            reply_to_message_id=msg.message_id,
        )

    return handler


async def _edit_delivered_messages(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    message_id: int,
    content_type: str,
    text: str | None,
    parse_mode: str | None,
) -> int:
    edited = 0
    for delivery in await repo.list_deliveries_for_message(message_id):
        if bool(delivery.get("deleted")):
            continue
        chat_id = int(delivery["recipient_id"])
        telegram_message_id = int(delivery["telegram_message_id"])
        try:
            if content_type == "text":
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=telegram_message_id,
                    text=text or "",
                    parse_mode=parse_mode,
                )
            elif content_type in {"photo", "video", "animation", "document"}:
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=telegram_message_id,
                    caption=text or "",
                    parse_mode=parse_mode,
                )
            else:
                continue
            edited += 1
        except Exception:
            logger.debug(
                "Failed to edit delivered message message_id=%s recipient_id=%s telegram_message_id=%s",
                message_id,
                chat_id,
                telegram_message_id,
                exc_info=True,
            )
    return edited


def _apply_sender_identity(payload: dict[str, Any], user: Any, username: str | None) -> dict[str, Any]:
    if payload.get("content_type") != "text":
        return payload
    if not payload.get("text") or not (user.sign_enabled or user.tripcode_enabled):
        return payload
    payload = dict(payload)
    body = html.escape(str(payload["text"]))
    if user.sign_enabled:
        label = f"@{html.escape(username)}" if username else "signed"
        payload["text"] = f"{body} <b><i>~ {label}</i></b>"
        payload["parse_mode"] = "HTML"
    elif user.tripcode_name and user.tripcode_hash:
        name = html.escape(str(user.tripcode_name))
        payload["text"] = f"<b>{name}</b> !{str(user.tripcode_hash)[:6]}\n{body}"
        payload["parse_mode"] = "HTML"
    return payload


def _cooldown_remaining_text(cooldown: Any) -> str:
    try:
        until = as_utc(str(cooldown["until_at"]))
        remaining = max(
            0, int((until - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return "active"
    if remaining >= 3600:
        hours, rem = divmod(remaining, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes}m"
    if remaining >= 60:
        minutes, seconds = divmod(remaining, 60)
        return f"{minutes}m {seconds}s"
    return f"{remaining}s"


async def _send_cooldown_message_to_mods(
    msg: Any,
    sender_id: int,
    username: str | None,
    cooldown: Any,
    repo: Any,
    cfg: dict[str, Any],
    queue: Any,
    sender_cache: SenderMetadataCache,
) -> None:
    payload = _extract_payload(msg)
    prefix = (
        f"<b>In cooldown:</b> "
        f"Remaining: {html.escape(_cooldown_remaining_text(cooldown))}. "
        f"Reason: {html.escape(str(cooldown['reason'] or 'cooldown'))}."
    )
    if payload.get("text"):
        payload["text"] = f"{prefix} {html.escape(str(payload['text']))}"
    else:
        payload["text"] = prefix
    payload["parse_mode"] = "HTML"

    reply_to_message_id = None
    if getattr(msg, "reply_to_message", None):
        reply_msg = msg.reply_to_message
        lookup = await repo.sender_by_delivery(sender_id, reply_msg.message_id)
        if lookup is not None:
            reply_to_message_id, _ = lookup

    message_id = await repo.create_message(
        sender_id=sender_id,
        content_type=payload["content_type"],
        text_content=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_kind=payload.get("media_kind"),
        source_chat_id=getattr(msg, "chat_id", None),
        source_message_id=getattr(msg, "message_id", None),
        reply_to_message_id=reply_to_message_id,
        parse_mode=payload.get("parse_mode"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        is_forward=bool(payload.get("is_forward")),
        forward_from_chat_id=payload.get("forward_from_chat_id"),
        forward_message_id=payload.get("forward_message_id"),
        sticker_set_name=payload.get("sticker_set_name"),
    )
    await repo.set_message_tag(message_id, "OK", "cooldown-visible-to-mods")
    sender_user = await repo.get_user(sender_id)
    sender_cache.set(
        message_id,
        CachedSenderMetadata(
            sender_id=sender_id,
            username=username,
            temporal_id=temporal_id(sender_id, cfg["bot"]["global_salt"]),
            role="user",
            credits=sender_user.credits if sender_user is not None else 0.0,
            cached_at=0.0,
        ),
    )
    recipients = [u for u in await repo.list_mod_and_admin_users() if u.telegram_id != sender_id]
    await queue.enqueue_batch(
        message_id=message_id,
        sender_id=sender_id,
        recipients=recipients,
        content_type=payload["content_type"],
        text_content=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_kind=payload.get("media_kind"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        is_system=True,
        reply_to_message_id=reply_to_message_id,
        include_remove_button=False,
        parse_mode=payload.get("parse_mode"),
        is_forward=bool(payload.get("is_forward")),
        forward_from_chat_id=payload.get("forward_from_chat_id"),
        forward_message_id=payload.get("forward_message_id"),
        media_hash=payload.get("media_hash"),
        media_hash_first_seen_at=payload.get("media_hash_first_seen_at"),
        mime_type=payload.get("mime_type"),
        is_animated=payload.get("is_animated"),
        is_video=payload.get("is_video"),
    )


async def _run_pipeline(
    msg: Any,
    sender_id: int,
    username: str | None,
    repo: Any,
    cfg: dict[str, Any],
    queue: Any,
    tagger: TaggingPipeline,
    state: EphemeralState,
    sender_cache: SenderMetadataCache,
    payload: dict[str, Any] | None = None,
) -> None:
    if payload is None:
        payload = _extract_payload(msg)
    user = await repo.get_user(sender_id)
    if user is None:
        return
    payload = _apply_sender_identity(payload, user, username)
    reply_to_message_id = None
    if getattr(msg, "reply_to_message", None):
        reply_msg = msg.reply_to_message
        lookup = await repo.sender_by_delivery(sender_id, reply_msg.message_id)
        if lookup is None:
            source = await repo.message_by_source(reply_msg.chat_id, reply_msg.message_id)
            if source is None:
                await msg.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            reply_to_message_id = int(source["id"])
        else:
            reply_to_message_id, _ = lookup
    message_id = await repo.create_message(
        sender_id=sender_id,
        content_type=payload["content_type"],
        text_content=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_kind=payload.get("media_kind"),
        source_chat_id=getattr(msg, "chat_id", None),
        source_message_id=getattr(msg, "message_id", None),
        reply_to_message_id=reply_to_message_id,
        parse_mode=payload.get("parse_mode"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        sticker_set_name=payload.get("sticker_set_name"),
    )
    media_info = None
    media_service = None
    bot = None
    media_bytes = None
    if hasattr(queue, "media_service"):
        media_service = queue.media_service
    if media_service is not None and payload.get("media_file_id"):
        bot = getattr(msg, "_bot", None)
        if bot is None and hasattr(msg, "get_bot"):
            bot = msg.get_bot()
        media_info = await media_service.inspect(
            bot,
            payload.get("media_file_id"),
            payload.get("media_kind"),
            payload.get("thumbnail_file_id"),
            mime_type=payload.get("mime_type"),
            is_animated=payload.get("is_animated"),
            is_video=payload.get("is_video"),
        )
        if media_info is not None and media_info.is_image_like and payload.get("media_kind") != "sticker":
            media_bytes = media_info.preview_bytes
            media_service.seed_blurred_image(
                payload.get("thumbnail_file_id") or payload.get("media_file_id"),
                media_bytes,
            )
    result = await tagger.run_once(
        payload.get("text"),
        payload.get("media_kind"),
        media_info,
        media_bytes,
        sticker_set_name=payload.get("sticker_set_name"),
        repo=repo,
        cfg=cfg,
        message_id=message_id,
    )
    await repo.set_message_tag(message_id, result.tag, result.reason)

    if result.tag == "BLOCKED":
        logger.info("Message blocked sender_id=%s message_id=%s reason=%s",
                    sender_id, message_id, result.reason)
        if result.reason == "blocked-sticker-set":
            await _distribute_blocked_sticker_to_mods(
                message_id,
                sender_id,
                payload,
                repo,
                queue,
                reply_to_message_id=reply_to_message_id,
            )
        reply = (
            Msg.INVITE_LINK_BLOCKED
            if result.reason == "telegram-invite-link"
            else Msg.STICKERPACK_BLOCKED_REPLY
            if result.reason == "blocked-sticker-set"
            else Msg.BLOCKED_REPLY
        )
        await msg.reply_text(reply, reply_to_message_id=msg.message_id)
        return
    if result.tag == "DUPLICATE":
        await msg.reply_text(Msg.DUPLICATE_MEDIA, reply_to_message_id=msg.message_id)
        return

    sender_cache.set(
        message_id,
        CachedSenderMetadata(
            sender_id=sender_id,
            username=username,
            temporal_id=temporal_id(sender_id, cfg["bot"]["global_salt"]),
            role="admin" if user.is_admin else "moderator" if user.is_moderator else "user",
            credits=user.credits,
            cached_at=0.0,
        ),
    )

    if result.tag == "QUESTIONABLE" and user.confirmation_enabled:
        logger.debug(
            "Questionable message sender_id=%s message_id=%s", sender_id, message_id)
        stored_message = await repo.get_message(message_id)
        if stored_message is not None:
            payload["media_hash"] = stored_message.get("media_hash")
            payload["media_hash_first_seen_at"] = stored_message.get("media_hash_first_seen_at")
        state.set_confirmation(
            message_id,
            {"sender_id": sender_id, "username": username, "payload": payload,
                "reply_to_message_id": reply_to_message_id},
        )
        await msg.reply_text(
            Msg.QUESTIONABLE_PROMPT,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(
                        Msg.CONFIRMATION_SEND_BUTTON, callback_data=f"confirm:{message_id}")],
                ]
            ),
            reply_to_message_id=msg.message_id,
        )
        return

    stored_message = await repo.get_message(message_id)
    if stored_message is not None:
        payload["media_hash"] = stored_message.get("media_hash")
        payload["media_hash_first_seen_at"] = stored_message.get("media_hash_first_seen_at")

    await _distribute(
        message_id,
        sender_id,
        payload,
        repo,
        queue,
        cfg,
        reply_to_message_id=reply_to_message_id,
        include_remove_button=result.tag == "QUESTIONABLE" or result.reason == "telegram-invite-described",
        tag=result.tag,
    )


async def _distribute(
    message_id: int,
    sender_id: int,
    payload: dict[str, Any],
    repo: Any,
    queue: Any,
    cfg: dict[str, Any],
    reply_to_message_id: int | None = None,
    include_remove_button: bool = False,
    tag: str = "OK",
) -> None:
    recipients = await repo.list_eligible_recipients(sender_id)
    if tag == "POTENTIALLY_UNWANTED":
        before_count = len(recipients)
        recipients = [
            user for user in recipients
            if (user.is_moderator or user.is_admin or not user.hide_potentially_unwanted)
        ]
        logger.debug(
            "Filtered potentially unwanted message_id=%s recipients_before=%s recipients_after=%s",
            message_id,
            before_count,
            len(recipients),
        )
    logger.debug(
        "Distributing message_id=%s sender_id=%s recipients=%s content_type=%s tag=%s reply_to=%s",
        message_id,
        sender_id,
        len(recipients),
        payload["content_type"],
        tag,
        reply_to_message_id,
    )
    if not recipients:
        logger.warning(
            "Message has no eligible recipients message_id=%s sender_id=%s content_type=%s tag=%s",
            message_id,
            sender_id,
            payload["content_type"],
            tag,
        )
    await queue.enqueue_batch(
        message_id=message_id,
        sender_id=sender_id,
        recipients=recipients,
        content_type=payload["content_type"],
        text_content=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_kind=payload.get("media_kind"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        is_system=False,
        reply_to_message_id=reply_to_message_id,
        include_remove_button=include_remove_button,
        parse_mode=payload.get("parse_mode"),
        is_forward=bool(payload.get("is_forward")),
        forward_from_chat_id=payload.get("forward_from_chat_id"),
        forward_message_id=payload.get("forward_message_id"),
        media_hash=payload.get("media_hash"),
        media_hash_first_seen_at=payload.get("media_hash_first_seen_at"),
        mime_type=payload.get("mime_type"),
        is_animated=payload.get("is_animated"),
        is_video=payload.get("is_video"),
    )


async def _distribute_blocked_sticker_to_mods(
    message_id: int,
    sender_id: int,
    payload: dict[str, Any],
    repo: Any,
    queue: Any,
    reply_to_message_id: int | None = None,
) -> None:
    mods = await repo.list_mod_and_admin_users()
    if not mods:
        return
    await queue.enqueue_batch(
        message_id=message_id,
        sender_id=sender_id,
        recipients=mods,
        content_type=payload["content_type"],
        text_content=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_kind=payload.get("media_kind"),
        thumbnail_file_id=payload.get("thumbnail_file_id"),
        is_system=True,
        reply_to_message_id=reply_to_message_id,
        include_remove_button=False,
        parse_mode=payload.get("parse_mode"),
        is_forward=bool(payload.get("is_forward")),
        forward_from_chat_id=payload.get("forward_from_chat_id"),
        forward_message_id=payload.get("forward_message_id"),
        media_hash=payload.get("media_hash"),
        media_hash_first_seen_at=payload.get("media_hash_first_seen_at"),
        mime_type=payload.get("mime_type"),
        is_animated=payload.get("is_animated"),
        is_video=payload.get("is_video"),
    )
    notice_id = await repo.create_message(
        sender_id=sender_id,
        content_type="text",
        text_content=Msg.STICKER_BLOCKED_MOD_NOTICE,
        media_file_id=None,
        media_kind=None,
        reply_to_message_id=message_id,
        parse_mode=None,
    )
    await repo.set_message_tag(notice_id, "OK", "blocked-sticker-mod-notice")
    await queue.enqueue_batch(
        message_id=notice_id,
        sender_id=sender_id,
        recipients=mods,
        content_type="text",
        text_content=Msg.STICKER_BLOCKED_MOD_NOTICE,
        media_file_id=None,
        media_kind=None,
        is_system=True,
        reply_to_message_id=message_id,
        include_remove_button=False,
    )
    reward = float(
        cfg_value(
            payload["content_type"],
            text_reward=float(cfg["credits"]["text_message_reward"]),
            media_reward=float(cfg["credits"]["media_message_reward"]),
        )
    )
    reward_reason = "text_message_reward" if payload["content_type"] == "text" else "media_message_reward"
    await adjust_credits_with_daily_limit(repo, cfg, sender_id, reward, reward_reason)
    await repo.touch_activity(sender_id)


def cfg_value(content_type: str, text_reward: float, media_reward: float) -> float:
    return text_reward if content_type == "text" else media_reward


def _retry(repo: Any, cfg: dict[str, Any], rate_limiter: Any, queue: Any, tagger: TaggingPipeline, state: EphemeralState, sender_cache: SenderMetadataCache):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        await q.answer()
        token = q.data.split(":", 1)[1]
        payload = state.pop_retry(token)
        if payload is None:
            await q.edit_message_text("Retry context expired.")
            return
        allowed, retry_in = rate_limiter.check(payload["sender_id"])
        if not allowed:
            state.set_retry(token, payload)
            await q.edit_message_text(
                Msg.RATE_LIMIT_REPLY.format(seconds=retry_in),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        Msg.RETRY_BUTTON, callback_data=f"retry:{token}")]]
                ),
            )
            return
        try:
            await q.delete_message()
        except Exception:
            pass

        class MessageProxy:
            def __init__(self, p: dict[str, Any], bot: Any) -> None:
                self.text = p.get("text")
                self.photo = None
                self.video = None
                self.animation = None
                self.sticker = None
                self.document = None
                self.caption = p.get("text")
                self._chat_id = p["sender_id"]
                self._bot = bot

            async def reply_text(self, text: str, **kwargs: Any) -> Any:
                return await self._bot.send_message(chat_id=self._chat_id, text=text, **kwargs)

        proxy = MessageProxy(payload, context.bot)
        await _run_pipeline(
            proxy,
            payload["sender_id"],
            payload.get("username"),
            repo,
            cfg,
            queue,
            tagger,
            state,
            sender_cache,
            payload=payload,
        )

    return handler


def _confirm(repo: Any, cfg: dict[str, Any], queue: Any, state: EphemeralState):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        await q.answer()
        message_id = int(q.data.split(":", 1)[1])
        pending = state.pop_confirmation(message_id)
        if pending is None:
            await q.edit_message_text("Confirmation expired.")
            return
        if await repo.get_message(message_id) is None:
            await q.edit_message_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        await _distribute(
            message_id,
            pending["sender_id"],
            pending["payload"],
            repo,
            queue,
            cfg,
            reply_to_message_id=pending.get("reply_to_message_id"),
            include_remove_button=True,
        )
        try:
            await q.delete_message()
        except Exception:
            await q.edit_message_text("Sent.")

    return handler


def _cancel(state: EphemeralState):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        await q.answer()
        message_id = int(q.data.split(":", 1)[1])
        state.pop_confirmation(message_id)
        try:
            await q.delete_message()
        except Exception:
            await q.edit_message_text(Msg.CONFIRMATION_CANCELLED)

    return handler


async def _send_auto_whisper_reply(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    cfg: dict[str, Any],
    sender: Any,
    original: Any,
    text: str,
    source_message_id: int | None,
) -> tuple[float, float]:
    sender_is_privileged = bool(sender.is_moderator or sender.is_admin)
    unlock = float(cfg["credits"]["whisper_unlock_credits"])
    if not sender_is_privileged and float(sender.credits) < unlock:
        raise RuntimeError(f"Whisper unlock requires {unlock:.2f} credits.")
    cost = 0.0 if sender_is_privileged or bool(original["is_modwhisper"]) else float(
        cfg["credits"]["whisper_cost"])
    if cost > 0 and float(sender.credits) < cost:
        raise RuntimeError("Insufficient credits for whisper.")
    original_sender = int(original["sender_id"])
    original_recipient = int(original["recipient_id"])
    target_id = original_recipient if sender.telegram_id == original_sender else original_sender
    is_modwhisper = False
    label = "Whisper"
    balance = float(sender.credits)
    if cost > 0:
        balance = await repo.adjust_credits(sender.telegram_id, -cost, "whisper_cost")
    await repo.touch_activity(sender.telegram_id)
    if target_id == sender.telegram_id:
        return cost, balance
    whisper_id = await repo.create_whisper(sender.telegram_id, target_id, text, is_modwhisper)
    if source_message_id is not None:
        await repo.add_whisper_delivery(whisper_id, sender.telegram_id, source_message_id)

    recipients = [target_id]
    for mod in await repo.list_mod_and_admin_users():
        if mod.telegram_id not in {sender.telegram_id, target_id}:
            recipients.append(mod.telegram_id)

    for recipient_id in recipients:
        reply_to = await repo.whisper_delivery_message_id(int(original["id"]), target_id)
        if recipient_id != target_id:
            reply_to = await repo.whisper_delivery_message_id(int(original["id"]), recipient_id)
        try:
            sent = await context.bot.send_message(
                chat_id=recipient_id,
                text=f"<i><b>{label}:</b></i> {html.escape(text)}",
                parse_mode="HTML",
                reply_to_message_id=reply_to,
            )
            await repo.add_whisper_delivery(whisper_id, recipient_id, sent.message_id)
        except Exception:
            pass
    return cost, balance


def _reaction_vote(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reaction = update.message_reaction
        if reaction is None or reaction.user is None or reaction.chat is None:
            logger.debug("Ignoring reaction update without user/chat: %s", reaction)
            return
        emojis = _reaction_emojis(getattr(reaction, "new_reaction", None))
        logger.debug(
            "Reaction update user_id=%s chat_id=%s message_id=%s emojis=%s",
            reaction.user.id,
            reaction.chat.id,
            reaction.message_id,
            sorted(emojis),
        )
        if not emojis:
            return
        delete_emoji = _normalize_reaction_emoji(
            str(cfg.get("moderation", {}).get("delete_reaction_emoji", "✍️"))
        )
        if delete_emoji in emojis:
            handled = await _handle_mod_delete_reaction(context, repo, cfg, reaction)
            if handled:
                return
        vote_type = None
        if emojis & {"👍", "🔥", "❤", "❤️", "💖", "💙", "💚", "💛", "🧡", "💜"}:
            vote_type = "upvote"
        elif emojis & {"👎"}:
            vote_type = "downvote"
        if vote_type is None:
            return
        voter_id = int(reaction.user.id)
        lookup = await repo.sender_by_delivery(voter_id, int(reaction.message_id))
        if lookup is None:
            whisper_ctx = await repo.whisper_context_by_reply(voter_id, int(reaction.message_id))
            if whisper_ctx is None:
                try:
                    await context.bot.send_message(chat_id=voter_id, text=Msg.MESSAGE_NOT_IN_CACHE)
                except Exception:
                    pass
                return
            message_id = -int(whisper_ctx["id"])
            sender_id = int(whisper_ctx["sender_id"])
        else:
            message_id, sender_id = lookup
        if sender_id == voter_id:
            try:
                await context.bot.send_message(chat_id=voter_id, text=Msg.VOTE_OWN)
            except Exception:
                pass
            return
        if await repo.has_any_vote(message_id, voter_id):
            try:
                await context.bot.send_message(chat_id=voter_id, text=Msg.VOTE_ALREADY)
            except Exception:
                pass
            return
        voter = await repo.get_user(voter_id)
        if voter is None:
            return
        cooldown = await repo.get_active_cooldown(voter_id)
        if cooldown is not None:
            try:
                await context.bot.send_message(
                    chat_id=voter_id,
                    text=Msg.cooldown_remaining_with_reason(
                        _cooldown_remaining_text(cooldown),
                        str(cooldown["reason"] or "cooldown"),
                    ),
                )
            except Exception:
                pass
            return
        if vote_type == "upvote":
            await _apply_upvote(context, repo, cfg, message_id, sender_id, voter)
        else:
            await _apply_downvote(context, repo, cfg, message_id, sender_id, voter)

    return handler


async def _handle_mod_delete_reaction(context: ContextTypes.DEFAULT_TYPE, repo: Any, cfg: dict[str, Any], reaction: Any) -> bool:
    moderator_id = int(reaction.user.id)
    caller = await repo.get_user(moderator_id)
    if caller is None or not (caller.is_moderator or caller.is_admin):
        return False
    lookup = await repo.sender_by_delivery(moderator_id, int(reaction.message_id))
    if lookup is None:
        try:
            await context.bot.send_message(chat_id=moderator_id, text=Msg.MESSAGE_NOT_IN_CACHE)
        except Exception:
            pass
        return True
    message_id, sender_id = lookup
    if await repo.get_message(message_id) is None:
        try:
            await context.bot.send_message(chat_id=moderator_id, text=Msg.MESSAGE_NOT_IN_CACHE)
        except Exception:
            pass
        return True
    await remove_message_with_tombstones(
        context,
        repo,
        cfg,
        message_id,
        sender_id,
        "deleted by admin" if caller.is_admin else "deleted by moderator",
    )
    await append_action_info_to_message_for_mods(
        context,
        repo,
        message_id,
        "This message was deleted by moderator action",
    )
    return True


def _reaction_emojis(reactions: Any) -> set[str]:
    result: set[str] = set()
    for item in reactions or []:
        emoji = getattr(item, "emoji", None)
        if emoji is None:
            kind = getattr(item, "type", None)
            emoji = getattr(kind, "emoji", None)
        if emoji is not None:
            result.add(_normalize_reaction_emoji(str(emoji)))
    return result


def _normalize_reaction_emoji(emoji: str) -> str:
    return unicodedata.normalize("NFC", emoji).replace("\ufe0f", "")


async def _apply_upvote(context: ContextTypes.DEFAULT_TYPE, repo: Any, cfg: dict[str, Any], message_id: int, sender_id: int, voter: Any) -> None:
    cost = float(cfg["credits"]["upvote_cost"])
    if voter.credits < cost:
        await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.VOTE_NO_CREDITS)
        return
    if not await repo.add_vote(message_id, voter.telegram_id, "upvote", cost):
        await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.UPVOTE_ALREADY)
        return
    remaining = await repo.adjust_credits(voter.telegram_id, -cost, "upvote_cost")
    await repo.touch_activity(voter.telegram_id)
    reward = float(cfg["credits"]["upvote_reward"])
    _, applied_reward = await adjust_credits_with_daily_limit(repo, cfg, sender_id, reward, "upvote_reward")
    await repo.increment_received_vote_count(sender_id, "upvote")
    await _notify_sender_vote(context, repo, sender_id, message_id, f"Your message received an upvote. +{applied_reward:.2f} credit.")
    await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.upvote_cast(cost, remaining))


async def _apply_downvote(context: ContextTypes.DEFAULT_TYPE, repo: Any, cfg: dict[str, Any], message_id: int, sender_id: int, voter: Any) -> None:
    prev_streak, prev_iso = await repo.get_downvote_state(voter.telegram_id)
    now = datetime.now(timezone.utc)
    elapsed_minutes = 999.0
    if prev_iso:
        try:
            prev = as_utc(prev_iso)
            elapsed_minutes = (now - prev).total_seconds() / 60.0
        except ValueError:
            elapsed_minutes = 999.0
    decayed = max(0.0, prev_streak - max(0.0, elapsed_minutes))
    current_minute = decayed + 1.0
    cost = interpolate_downvote_cost(
        cfg["credits"]["downvote_cost_schedule"],
        current_minute,
        float(cfg["credits"]["downvote_start_cost"]),
    )
    if voter.credits < cost:
        await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.DOWNVOTE_NO_CREDITS)
        return
    if not await repo.add_vote(message_id, voter.telegram_id, "downvote", cost):
        await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.DOWNVOTE_ALREADY)
        return
    await repo.set_downvote_state(voter.telegram_id, current_minute, now.isoformat())
    remaining = await repo.adjust_credits(voter.telegram_id, -cost, "downvote_cost")
    await repo.touch_activity(voter.telegram_id)
    penalty = float(cfg["credits"]["downvote_penalty"])
    sender_balance = await repo.adjust_credits(sender_id, -penalty, "downvote_penalty")
    await repo.increment_received_vote_count(sender_id, "downvote")
    await apply_negative_credit_cooldown(repo, cfg, sender_id, sender_balance, voter.telegram_id)
    await _notify_sender_vote(context, repo, sender_id, message_id, f"Your message received a downvote. -{penalty:.2f} credit.")
    next_cost = interpolate_downvote_cost(
        cfg["credits"]["downvote_cost_schedule"],
        current_minute + 1.0,
        float(cfg["credits"]["downvote_start_cost"]),
    )
    await context.bot.send_message(chat_id=voter.telegram_id, text=Msg.downvote_cast(cost, remaining, next_cost))


async def _notify_sender_vote(context: ContextTypes.DEFAULT_TYPE, repo: Any, sender_id: int, message_id: int, text: str) -> None:
    sender = await repo.get_user(sender_id)
    if sender is None or not sender.votes_enabled:
        return
    upvotes, downvotes = await repo.get_received_vote_counts(sender_id)
    original = await repo.get_message(message_id)
    reply_to = None
    if original is not None and original["source_chat_id"] == sender_id:
        reply_to = original["source_message_id"]
    try:
        details = (
            f"{text}\n"
            f"Credits: {sender.credits:.2f}\n"
            f"Votes received: +{upvotes} / -{downvotes}"
        )
        await context.bot.send_message(chat_id=sender_id, text=details, reply_to_message_id=reply_to)
    except Exception:
        pass


def _delete_vote_callback(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.message is None or q.from_user is None:
            return
        await q.answer()
        message_id = int(q.data.split(":", 1)[1])
        allowed, reason = await check_remove_vote_allowed(repo, cfg, q.from_user.id)
        if not allowed:
            text = reason or "Remove vote unavailable."
            cooldown_left = _remove_vote_cooldown_seconds(text)
            if cooldown_left is not None:
                try:
                    await _set_delete_vote_cooldown_button(context, q, message_id, cooldown_left)
                except Exception:
                    logger.debug(
                        "Failed to update delete-vote cooldown button message_id=%s voter_id=%s",
                        message_id,
                        q.from_user.id,
                        exc_info=True,
                    )
                await q.answer(text)
                return
            await q.answer(text, show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id=q.from_user.id,
                    text=text,
                    reply_to_message_id=q.message.message_id,
                )
            except Exception:
                pass
            return
        message = await repo.get_message(message_id)
        if message is None:
            await q.answer(Msg.MESSAGE_NOT_IN_CACHE, show_alert=True)
            return
        sender_id = int(message["sender_id"])
        if sender_id == q.from_user.id:
            await q.answer(Msg.DELETEVOTE_OWN, show_alert=True)
            return
        if not await repo.add_remove_vote(message_id, q.from_user.id):
            await q.answer(Msg.DELETEVOTE_ALREADY_SHORT, show_alert=True)
            try:
                await _remove_delete_vote_button(context, q)
            except Exception:
                pass
            return
        await repo.touch_activity(q.from_user.id)
        try:
            await _remove_delete_vote_button(context, q)
        except Exception:
            pass
        count = await repo.count_remove_votes(message_id)
        threshold = int(cfg["vote_to_remove"]["threshold"])
        if count < threshold:
            text = Msg.remove_vote_counted(count, threshold)
            await q.answer(text)
            try:
                await context.bot.send_message(
                    chat_id=q.from_user.id,
                    text=text,
                    reply_to_message_id=q.message.message_id,
                )
            except Exception:
                pass
            return
        await remove_message_with_tombstones(context, repo, cfg, message_id, sender_id, "community vote threshold reached")
        await _notify_remove_voters(
            context,
            repo,
            message_id,
            Msg.DELETEVOTE_DELETED_NOTIFY,
        )
        collateral = int(cfg["vote_to_remove"]["collateral_remove_amount"])
        if collateral > 0:
            neighbors = await repo.list_neighbor_messages_by_sender(sender_id, message_id, collateral)
            for n in neighbors:
                await remove_message_with_tombstones(
                    context,
                    repo,
                    cfg,
                    int(n["id"]),
                    sender_id,
                    "collateral removal",
                )
        await q.answer(Msg.MESSAGE_REMOVED)

    return handler


def _remove_vote_cooldown_seconds(text: str) -> int | None:
    marker = "Remaining:"
    if marker in text:
        tail = text.split(marker, 1)[1].strip()
        number = tail.split("s", 1)[0].strip()
        try:
            return max(0, int(number))
        except ValueError:
            return None
    marker = "cooldown active ("
    if marker in text:
        tail = text.split(marker, 1)[1].strip()
        number = tail.split("s", 1)[0].strip()
        try:
            return max(0, int(number))
        except ValueError:
            return None
    return None


async def _remove_delete_vote_button(context: ContextTypes.DEFAULT_TYPE, q: Any) -> None:
    if q.message is None:
        return
    await context.bot.edit_message_reply_markup(
        chat_id=q.message.chat_id,
        message_id=q.message.message_id,
        reply_markup=None,
    )


async def _set_delete_vote_cooldown_button(
    context: ContextTypes.DEFAULT_TYPE,
    q: Any,
    message_id: int,
    seconds: int,
) -> None:
    if q.message is None:
        return
    await context.bot.edit_message_reply_markup(
        chat_id=q.message.chat_id,
        message_id=q.message.message_id,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"Vote to remove (Cooldown left: {seconds}s)",
                callback_data=f"rm:{message_id}",
            )]]
        ),
    )


def _mod_confirm(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        caller = await repo.get_user(q.from_user.id)
        if caller is None or not (caller.is_moderator or caller.is_admin):
            await q.answer(Msg.MOD_ONLY, show_alert=True)
            return
        _, msg_id_s, sender_s = q.data.split(":", 2)
        message_id = int(msg_id_s)
        sender_id = int(sender_s)
        message = await repo.get_message(message_id)
        if message is not None and bool(message.get("punishment_confirmed")):
            await q.answer(Msg.PUNISHMENT_CONFIRMED, show_alert=True)
            return
        if message is not None and bool(message.get("removed_for_mods")):
            await q.answer(Msg.REMOVED_FOR_MODS_ALREADY, show_alert=True)
            return
        if message is not None and bool(message.get("reverted")):
            await q.answer(Msg.REMOVAL_REVERTED_ALREADY, show_alert=True)
            return
        await _apply_remove_punishment(context, repo, cfg, message_id, sender_id, q.from_user.id)
        await repo.set_message_moderation_state(message_id, punishment_confirmed=True)
        await append_action_info_to_message_for_mods(
            context,
            repo,
            message_id,
            "This message was confirmed for removal",
        )
        await refresh_moderation_notes(context, repo, cfg, message_id, sender_id)
        try:
            await q.answer(Msg.CONFIRMED_PUNISHMENT_APPLIED)
        except Exception:
            pass

    return handler


async def _apply_remove_punishment(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    cfg: dict[str, Any],
    message_id: int,
    sender_id: int,
    moderator_id: int,
) -> None:
    sender = await repo.get_user(sender_id)
    if sender is None:
        return
    tax_pct = float(cfg["vote_to_remove"]["punishment_credit_tax_percent"])
    tax = max(
        float(cfg["vote_to_remove"].get("punishment_credit_minimum", 10.0)),
        sender.credits * tax_pct,
    )
    balance = await repo.adjust_credits(sender_id, -tax, "remove_punishment_tax")
    await apply_negative_credit_cooldown(repo, cfg, sender_id, balance, moderator_id)
    seconds = int(cfg["vote_to_remove"]["punishment_cooldown_seconds"])
    until = datetime.now(timezone.utc).replace(
        tzinfo=timezone.utc) + timedelta(seconds=seconds)
    await repo.set_cooldown(sender_id, until.isoformat(), "remove-punishment", moderator_id)
    message = await repo.get_message(message_id)
    reply_to = None
    if message is not None and message.get("source_chat_id") == sender_id:
        reply_to = message.get("source_message_id")
    try:
        await context.bot.send_message(
            chat_id=sender_id,
            text=Msg.removal_punishment(tax, balance, seconds),
            reply_to_message_id=reply_to,
        )
    except Exception:
        pass


async def _notify_remove_voters(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    message_id: int,
    text: str,
    skip_user_ids: set[int] | None = None,
    skip_mods: bool = False,
) -> None:
    skip_user_ids = skip_user_ids or set()
    voters = await repo.list_remove_voters(message_id)
    for voter_id in voters:
        if voter_id in skip_user_ids:
            continue
        if skip_mods:
            voter = await repo.get_user(voter_id)
            if voter is not None and (voter.is_moderator or voter.is_admin):
                continue
        reply_to = await repo.delivery_or_tombstone_message_for_recipient(message_id, voter_id)
        try:
            await context.bot.send_message(
                chat_id=voter_id,
                text=text,
                reply_to_message_id=reply_to,
            )
        except Exception:
            pass


def _mod_remove_for_mods(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.from_user is None:
            return
        caller = await repo.get_user(q.from_user.id)
        if caller is None or not (caller.is_moderator or caller.is_admin):
            await q.answer(Msg.MOD_ONLY, show_alert=True)
            return
        if q.data is not None:
            try:
                _, msg_id_s, sender_s = q.data.split(":", 2)
                message_id = int(msg_id_s)
                sender_id = int(sender_s)
                message = await repo.get_message(message_id)
                if message is not None and bool(message.get("removed_for_mods")):
                    await q.answer(Msg.REMOVED_FOR_MODS_SHORT, show_alert=True)
                    return
                if message is not None and bool(message.get("reverted")):
                    await q.answer(Msg.REMOVAL_REVERTED_ALREADY, show_alert=True)
                    return
                cfg = context.application.bot_data["cfg"]
                if message is None or not bool(message.get("punishment_confirmed")):
                    await _apply_remove_punishment(context, repo, cfg, message_id, sender_id, q.from_user.id)
                removed = await remove_message_for_mods(
                    context,
                    repo,
                    cfg,
                    message_id,
                    sender_id,
                    "removed for moderators",
                )
                await repo.set_message_moderation_state(
                    message_id,
                    punishment_confirmed=True,
                    removed_for_mods=True,
                )
                await refresh_moderation_notes(
                    context,
                    repo,
                    cfg,
                    message_id,
                    sender_id,
                )
                await q.answer(Msg.removed_for_mods(removed))
            except Exception:
                pass

    return handler


def _mod_revert(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        caller = await repo.get_user(q.from_user.id)
        if caller is None or not (caller.is_moderator or caller.is_admin):
            await q.answer(Msg.MOD_ONLY, show_alert=True)
            return
        _, msg_id_s, sender_s = q.data.split(":", 2)
        message_id = int(msg_id_s)
        sender_id = int(sender_s)
        message = await repo.get_message(message_id)
        if message is not None and bool(message.get("reverted")):
            await q.answer(Msg.REVERTED_ALREADY, show_alert=True)
            return
        if message is not None and bool(message.get("punishment_confirmed")):
            await q.answer(Msg.PUNISHMENT_CONFIRMED, show_alert=True)
            return
        voters = await repo.list_remove_voters(message_id)
        if not voters:
            await q.answer(Msg.NO_VOTERS, show_alert=True)
            return
        pct = float(cfg["vote_to_remove"].get(
            "reversal_punishment_credit_tax_percent", 0.1))
        for voter_id in voters:
            voter = await repo.get_user(voter_id)
            if voter is None:
                continue
            tax = max(
                float(cfg["vote_to_remove"].get(
                    "reversal_punishment_credit_minimum", 10.0)),
                voter.credits * pct,
            )
            balance = await repo.adjust_credits(voter_id, -tax, "remove_reversal_punishment")
            await apply_negative_credit_cooldown(repo, cfg, voter_id, balance, q.from_user.id)
            reply_to = await repo.delivery_message_for_recipient(message_id, voter_id)
            try:
                await context.bot.send_message(
                    chat_id=voter_id,
                    text=Msg.reversal_punishment(tax, balance),
                    reply_to_message_id=reply_to,
                )
            except Exception:
                pass
        reply_to = None
        if message is not None and message.get("source_chat_id") == sender_id:
            reply_to = message.get("source_message_id")
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=Msg.REVERT_SUCCESS,
                reply_to_message_id=reply_to,
            )
        except Exception:
            pass
        await repo.set_message_moderation_state(message_id, reverted=True)
        await append_action_info_to_message_for_mods(
            context,
            repo,
            message_id,
            "Mods did not confirm anything wrong with this message and reverted the removal",
        )
        await refresh_moderation_notes(context, repo, cfg, message_id, sender_id)
        await q.answer(Msg.REVERSAL_APPLIED)

    return handler


def _delete_confirm(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        caller = await repo.get_user(q.from_user.id)
        if caller is None or not (caller.is_moderator or caller.is_admin):
            await q.answer(Msg.MOD_ONLY, show_alert=True)
            return
        _, msg_id_s, sender_s = q.data.split(":", 2)
        message_id = int(msg_id_s)
        sender_id = int(sender_s)
        message = await repo.get_message(message_id)
        if message is None:
            await q.answer(Msg.MESSAGE_NOT_IN_CACHE, show_alert=True)
            try:
                await q.edit_message_text(Msg.MESSAGE_NOT_IN_CACHE)
            except Exception:
                pass
            return
        await remove_message_with_tombstones(
            context,
            repo,
            cfg,
            message_id,
            sender_id,
            "deleted by moderator",
        )
        await append_action_info_to_message_for_mods(
            context,
            repo,
            message_id,
            "This message was deleted by moderator action",
        )
        try:
            await q.edit_message_text("Message deleted.")
        except Exception:
            pass
        await q.answer(Msg.MESSAGE_DELETED)

    return handler


def _delete_cancel(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.from_user is None:
            return
        caller = await repo.get_user(q.from_user.id)
        if caller is None or not (caller.is_moderator or caller.is_admin):
            await q.answer(Msg.MOD_ONLY, show_alert=True)
            return
        try:
            await q.edit_message_text("Deletion cancelled.")
        except Exception:
            pass
        await q.answer(Msg.DELETION_CANCELLED)

    return handler


def _fight_accept(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        fight_id = int(q.data.split(":", 1)[1])
        req = await repo.get_fight_request(fight_id)
        if req is None or req["status"] != "PENDING":
            await q.answer(Msg.FIGHT_UNAVAILABLE_CALLBACK, show_alert=True)
            return
        try:
            exp = as_utc(str(req["expires_at"]))
            if exp < datetime.now(timezone.utc):
                await repo.set_fight_status(fight_id, "EXPIRED")
                await q.answer(Msg.FIGHT_REQUEST_EXPIRED, show_alert=True)
                return
        except ValueError:
            pass
        if int(req["recipient_id"]) != q.from_user.id:
            await q.answer(Msg.FIGHT_NOT_YOURS, show_alert=True)
            return
        stake = float(req["stake"])
        init_id = int(req["initiator_id"])
        recip_id = int(req["recipient_id"])
        initiator = await repo.get_user(init_id)
        recipient = await repo.get_user(recip_id)
        if initiator is None or recipient is None:
            await repo.set_fight_status(fight_id, "EXPIRED")
            await q.answer(Msg.FIGHT_EXPIRED, show_alert=True)
            return
        if initiator.credits < stake or recipient.credits < stake:
            await repo.set_fight_status(fight_id, "EXPIRED")
            await q.answer(Msg.FIGHT_ACCEPT_FAILED, show_alert=True)
            try:
                await context.bot.send_message(chat_id=init_id, text=Msg.FIGHT_ACCEPT_FAILED_NOTIFY)
            except Exception:
                pass
            return

        def tier(c: float) -> int:
            return int(math.log2(max(1.0, c)))

        diff = tier(initiator.credits) - tier(recipient.credits)
        p_init = max(0.1, min(0.9, 0.5 + 0.1 * diff))
        init_wins = random.random() < p_init
        winner = init_id if init_wins else recip_id
        loser = recip_id if init_wins else init_id
        win_tax = float(cfg["fights"]["win_tax_percent"])
        win_gain = stake * (1.0 - win_tax)
        lose_amt = stake
        _, actual_win_gain = await adjust_credits_with_daily_limit(repo, cfg, winner, win_gain, "fight_win")
        loser_balance = await repo.adjust_credits(loser, -lose_amt, "fight_loss")
        await apply_negative_credit_cooldown(repo, cfg, loser, loser_balance, winner)
        await repo.touch_activity(init_id)
        await repo.touch_activity(recip_id)
        await repo.set_fight_status(fight_id, "COMPLETED")

        matchup = "even match"
        if abs(diff) == 1:
            matchup = "slight advantage"
        elif abs(diff) >= 2:
            matchup = "advantage"

        if init_wins:
            init_delta, recip_delta = actual_win_gain, -lose_amt
        else:
            init_delta, recip_delta = -lose_amt, actual_win_gain
        updated_initiator = await repo.get_user(init_id)
        updated_recipient = await repo.get_user(recip_id)
        init_new = updated_initiator.credits if updated_initiator else 0.0
        recip_new = updated_recipient.credits if updated_recipient else 0.0
        try:
            await context.bot.send_message(
                chat_id=init_id,
                text=Msg.fight_result(
                    init_wins, init_delta, init_new, matchup),
            )
            await context.bot.send_message(
                chat_id=recip_id,
                text=Msg.fight_result(
                    not init_wins, recip_delta, recip_new, matchup),
            )
        except Exception:
            pass
        try:
            await q.edit_message_text("Fight accepted and resolved.")
        except Exception:
            pass
        await q.answer()

    return handler


def _fight_decline(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        fight_id = int(q.data.split(":", 1)[1])
        req = await repo.get_fight_request(fight_id)
        if req is None or req["status"] != "PENDING":
            await q.answer(Msg.FIGHT_UNAVAILABLE_CALLBACK, show_alert=True)
            return
        if int(req["recipient_id"]) != q.from_user.id:
            await q.answer(Msg.FIGHT_NOT_YOURS, show_alert=True)
            return
        await repo.set_fight_status(fight_id, "DECLINED")
        await repo.touch_activity(q.from_user.id)
        try:
            await context.bot.send_message(
                chat_id=int(req["initiator_id"]),
                text=Msg.FIGHT_DECLINED,
                reply_to_message_id=req.get("initiator_message_id"),
            )
        except Exception:
            pass
        try:
            await q.edit_message_text("Fight declined.")
        except Exception:
            pass
        await q.answer()

    return handler


def _extract_payload(msg: Any) -> dict[str, Any]:
    base = {}
    if _should_preserve_forward_origin(msg):
        base = {
            "is_forward": True,
            "forward_from_chat_id": getattr(msg, "chat_id", None),
            "forward_message_id": getattr(msg, "message_id", None),
        }
    if getattr(msg, "text", None):
        return base | {"content_type": "text", "text": msg.text, "media_file_id": None, "media_kind": None}
    if getattr(msg, "photo", None):
        return base | {
            "content_type": "photo",
            "text": getattr(msg, "caption", None),
            "media_file_id": msg.photo[-1].file_id,
            "media_kind": "photo",
            "thumbnail_file_id": msg.photo[0].file_id if len(msg.photo) > 1 else None,
        }
    if getattr(msg, "video", None):
        return base | {
            "content_type": "video",
            "text": getattr(msg, "caption", None),
            "media_file_id": msg.video.file_id,
            "media_kind": "video",
            "thumbnail_file_id": msg.video.thumbnail.file_id if getattr(msg.video, "thumbnail", None) else None,
            "mime_type": getattr(msg.video, "mime_type", None),
        }
    if getattr(msg, "animation", None):
        return base | {
            "content_type": "animation",
            "text": getattr(msg, "caption", None),
            "media_file_id": msg.animation.file_id,
            "media_kind": "animation",
            "thumbnail_file_id": msg.animation.thumbnail.file_id if getattr(msg.animation, "thumbnail", None) else None,
            "mime_type": getattr(msg.animation, "mime_type", None),
        }
    if getattr(msg, "sticker", None):
        return base | {
            "content_type": "sticker",
            "text": None,
            "media_file_id": msg.sticker.file_id,
            "media_kind": "sticker",
            "thumbnail_file_id": msg.sticker.thumbnail.file_id if getattr(msg.sticker, "thumbnail", None) else None,
            "sticker_set_name": getattr(msg.sticker, "set_name", None),
            "is_animated": getattr(msg.sticker, "is_animated", None),
            "is_video": getattr(msg.sticker, "is_video", None),
        }
    if getattr(msg, "document", None):
        return base | {
            "content_type": "document",
            "text": getattr(msg, "caption", None),
            "media_file_id": msg.document.file_id,
            "media_kind": "document",
            "thumbnail_file_id": msg.document.thumbnail.file_id if getattr(msg.document, "thumbnail", None) else None,
            "mime_type": getattr(msg.document, "mime_type", None),
        }
    if getattr(msg, "video_note", None):
        return base | {
            "content_type": "video_note",
            "text": None,
            "media_file_id": msg.video_note.file_id,
            "media_kind": "video_note",
            "thumbnail_file_id": msg.video_note.thumbnail.file_id if getattr(msg.video_note, "thumbnail", None) else None,
        }
    return base | {"content_type": "text", "text": "", "media_file_id": None, "media_kind": None}


def _should_preserve_forward_origin(msg: Any) -> bool:
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return False
    if bool(getattr(msg, "has_protected_content", False)):
        return False
    origin_type = str(getattr(origin, "type", "") or "").lower()
    if origin_type == "hidden_user":
        return False
    if origin_type in {"user", "chat", "channel"}:
        return True
    if getattr(origin, "sender_user", None) is not None:
        return True
    if getattr(origin, "chat", None) is not None:
        return True
    return False
