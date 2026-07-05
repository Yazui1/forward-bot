from __future__ import annotations

import logging
from typing import Any

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from forward_bot.cache.transient import TransientStore
from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.identity import display_identity, display_identity_html, resolve_user_reference
from forward_bot.logging_utils import log_telegram_error


LOGGER = logging.getLogger(__name__)


def services(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.application.bot_data


def get_repo(context: ContextTypes.DEFAULT_TYPE) -> Repository:
    return services(context)["repo"]


def get_config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return services(context)["config_ref"]["config"]


def get_store(context: ContextTypes.DEFAULT_TYPE) -> TransientStore:
    return services(context)["store"]


def touch_activity(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    repo = get_repo(context)
    repo.touch_activity(user_id)
    queue = context.application.bot_data.get("queue")
    if queue and hasattr(queue, "on_user_activity"):
        queue.on_user_activity(user_id)


def args_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[User | None, bool]:
    tg = update.effective_user
    if not tg:
        return None, False
    config = get_config(context)
    repo = get_repo(context)
    return repo.ensure_user(
        tg.id,
        tg.username,
        float(config.get("credits.starting_balance", 20.0) or 20.0),
        config.get("bot.admin_ids", []) or [],
    )


def is_mod(user: User | None) -> bool:
    return bool(user and (user.is_moderator or user.is_admin))


def is_admin(user: User | None) -> bool:
    return bool(user and user.is_admin)


async def resolve_replied_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str | None]:
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not update.effective_user:
        return None, "Reply to a message."
    store = get_store(context)
    delivery = store.resolve_delivery(update.effective_user.id, msg.reply_to_message.message_id)
    if delivery:
        tm = store.get_message(delivery.message_id)
        if tm and tm.sender_id:
            return tm.sender_id, None
    mod_note_msg = store.resolve_mod_note(update.effective_user.id, msg.reply_to_message.message_id)
    if mod_note_msg and mod_note_msg.sender_id:
        return mod_note_msg.sender_id, None
    wdel = store.resolve_whisper_delivery(update.effective_user.id, msg.reply_to_message.message_id)
    if wdel:
        whisper = store.whispers.get(wdel.whisper_id)
        if whisper:
            other = whisper.sender_id if whisper.sender_id != update.effective_user.id else whisper.target_id
            return other, None
    own = store.resolve_source(msg.chat_id, msg.reply_to_message.message_id)
    if own and own.sender_id:
        return own.sender_id, None
    return None, "Message is not in cache anymore."


async def resolve_message_from_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.reply_to_message or not user:
        return None, None, "Reply to a message."
    store = get_store(context)
    delivery = store.resolve_delivery(user.id, msg.reply_to_message.message_id)
    if delivery:
        return store.get_message(delivery.message_id), delivery, None
    mod_note_msg = store.resolve_mod_note(user.id, msg.reply_to_message.message_id)
    if mod_note_msg:
        return mod_note_msg, None, None
    own = store.resolve_source(msg.chat_id, msg.reply_to_message.message_id)
    if own:
        return own, None, None
    return None, None, "Message is not in cache anymore."


async def reply_to_for_target(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int) -> int | None:
    msg = update.effective_message
    viewer = update.effective_user
    if not msg or not msg.reply_to_message or not viewer:
        return None
    store = get_store(context)
    normal_msg, _, _ = await resolve_message_from_reply(update, context)
    if normal_msg:
        if normal_msg.sender_id == target_id and normal_msg.source_chat_id == target_id:
            return normal_msg.source_message_id
        return store.delivery_reply_for_recipient(normal_msg.id, target_id)
    wdel = store.resolve_whisper_delivery(viewer.id, msg.reply_to_message.message_id)
    if wdel:
        delivery = next((d for d in store.deliveries_for_whisper(wdel.whisper_id) if d.recipient_id == target_id), None)
        return delivery.telegram_message_id if delivery else None
    return None


async def resolve_target_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    viewer: User | None,
    *,
    arg_index: int = 0,
    prefer_reply: bool = True,
) -> tuple[User | None, str | None, str]:
    repo = get_repo(context)
    config = get_config(context)
    msg = update.effective_message
    args = context.args or []
    if prefer_reply and msg and msg.reply_to_message:
        target_id, error = await resolve_replied_sender(update, context)
        target = repo.get_user(target_id) if target_id else None
        return target, None if target else error, " ".join(args[arg_index:]).strip()
    if len(args) > arg_index:
        target = resolve_user_reference(repo, config, args[arg_index], viewer)
        return target, None if target else "User not found or not visible.", " ".join(args[arg_index + 1 :]).strip()
    return None, "Reply to a user/message or pass a user reference.", ""


async def reply_in_context(message: Message | None, text: str, **kwargs: Any) -> None:
    if not message:
        return
    try:
        await message.reply_text(text, **kwargs)
    except TelegramError as exc:
        log_telegram_error(LOGGER, "command.reply_in_context", exc, chat_id=message.chat_id, message_id=message.message_id)
        pass


async def command_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *, prefer_target: bool = True, **kwargs: Any) -> None:
    msg = update.effective_message
    if not msg:
        return
    has_context_target = bool(prefer_target and msg.reply_to_message)
    reply_to = msg.reply_to_message.message_id if has_context_target and msg.reply_to_message else msg.message_id
    try:
        await context.bot.send_message(chat_id=msg.chat_id, text=text, reply_to_message_id=reply_to, **kwargs)
    except TelegramError as exc:
        aggregate = context.application.bot_data.get("aggregate_logger")
        log_telegram_error(LOGGER, "command.reply", exc, aggregate=aggregate, repo=get_repo(context), user_id=msg.chat_id, chat_id=msg.chat_id, message_id=msg.message_id, reply_to=reply_to)
        if has_context_target:
            try:
                await context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=f"{text}\n\nThe referenced message is unavailable, so this reply is attached to your command instead.",
                    reply_to_message_id=msg.message_id,
                    **kwargs,
                )
            except TelegramError as fallback_exc:
                log_telegram_error(LOGGER, "command.reply_fallback", fallback_exc, aggregate=aggregate, repo=get_repo(context), user_id=msg.chat_id, chat_id=msg.chat_id, message_id=msg.message_id)
                pass
            return
        await reply_in_context(msg, text, **kwargs)
