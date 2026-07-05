from __future__ import annotations

import logging
from collections import Counter

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from forward_bot.commands.common import (
    args_text,
    command_reply,
    display_identity_html,
    ensure_user,
    get_config,
    get_repo,
    get_store,
    is_admin,
    is_mod,
    reply_to_for_target,
    resolve_message_from_reply,
    resolve_target_user,
)
from forward_bot.commands.help_registry import HelpRegistry
from forward_bot.config import Config
from forward_bot.features.credits import loss_rate, tax_rate
from forward_bot.features.tombstones import mark_for_moderation_action, remove_message, remove_whisper
from forward_bot.logging_utils import log_telegram_error
from forward_bot.utils import html_escape, human_seconds, now_utc, parse_dt, parse_duration_seconds


LOGGER = logging.getLogger(__name__)


def register_mod_commands(registry: HelpRegistry) -> None:
    add = registry.add
    add("togglemod", "Admin", "Toggle moderator status.", togglemod, admin=True)
    add("mod", "Admin", "Promote a moderator.", mod, admin=True)
    add("unmod", "Admin", "Demote a moderator.", unmod, admin=True)
    add("ban", "Admin", "Ban a user.", ban, admin=True)
    add("unban", "Admin", "Unban a user.", unban, admin=True)
    add("purgebanned", "Admin", "Remove cached messages from banned users.", purgebanned, admin=True)
    add("adminsay", "Admin", "Urgently broadcast as admin.", adminsay, admin=True)
    add("reload", "Admin", "Reload config without restarting workers.", reload_config, admin=True)
    add("status", "Admin", "Show delivery queue and cache status.", status, admin=True)
    add("warn", "Moderation", "Warn a user by reply or reference.", warn, mod=True)
    add("cooldown", "Moderation", "Apply a user cooldown.", cooldown, mod=True)
    add("uncooldown", "Moderation", "Clear a user cooldown.", uncooldown, mod=True)
    add("moderated", "Moderation", "List moderated users and cooldowns.", moderated, mod=True)
    add("delete", "Moderation", "Delete a message; mods must confirm.", delete, mod=True)
    add("blocksticker", "Moderation", "Block a sticker pack by reply.", blocksticker, mod=True)
    add("modsay", "Moderation", "Urgently broadcast as mods.", modsay, mod=True)


async def _require_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, _ = await ensure_user(update, context)
    if not is_mod(user):
        await update.effective_message.reply_text("You are not allowed to do that.")
        return None
    return user


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, _ = await ensure_user(update, context)
    if not is_admin(user):
        await update.effective_message.reply_text("Admin only.")
        return None
    return user


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, viewer, *, args_start: int = 0):
    return await resolve_target_user(update, context, viewer, arg_index=args_start)


def args_text_from(context: ContextTypes.DEFAULT_TYPE, start: int) -> str:
    return " ".join((context.args or [])[start:]).strip()


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    queue = context.application.bot_data.get("queue")
    if not queue or not hasattr(queue, "snapshot"):
        await command_reply(update, context, "Delivery queue is not available.", prefer_target=False)
        return
    text = _status_report(queue.snapshot(), get_repo(context), get_store(context))
    await command_reply(update, context, text, prefer_target=False)


def _status_report(snapshot: dict, repo, store) -> str:
    users = repo.list_users()
    active_window = int(snapshot.get("active_window_seconds", 0) or 0)
    now = now_utc()
    started = [user for user in users if user.has_started and not user.is_banned]
    active = 0
    inactive = 0
    never_active = 0
    for user in started:
        last = parse_dt(user.last_activity)
        if not last:
            never_active += 1
        elif active_window and (now - last).total_seconds() <= active_window:
            active += 1
        else:
            inactive += 1

    message_types = Counter(message.content_type for message in store.messages.values())
    delivery_deleted = sum(1 for delivery in store.deliveries.values() if delivery.deleted)
    whisper_delivery_deleted = sum(1 for delivery in store.whisper_deliveries.values() if delivery.deleted)
    message_deleted = sum(1 for message in store.messages.values() if message.deleted)
    removed_for_mods = sum(1 for message in store.messages.values() if message.removed_for_mods)
    system_messages = sum(1 for message in store.messages.values() if message.is_system)
    urgent_messages = sum(1 for message in store.messages.values() if message.urgent)
    mod_whispers = sum(1 for whisper in store.whispers.values() if whisper.is_modwhisper)
    mod_note_copies = sum(len(notes) for notes in store.mod_notes.values())

    workers = snapshot.get("workers", []) or []
    alive_workers = sum(1 for worker in workers if not worker.get("done") and not worker.get("cancelled"))
    inflight = [
        f"{worker.get('name')} msg={worker.get('inflight_message_id')} to={worker.get('inflight_recipient_id')}"
        for worker in workers
        if worker.get("inflight_message_id") is not None
    ]

    lines = [
        "Bot status",
        f"Queue: {'running' if snapshot.get('running') else 'stopped'}; stopping={snapshot.get('stopping')}",
        f"Workers: {alive_workers}/{snapshot.get('worker_count_config', len(workers))} alive; inflight={snapshot.get('inflight', 0)}",
        f"Open items: {snapshot.get('open_items', 0)} total; heap={snapshot.get('heap_live', 0)} live/{snapshot.get('heap_cancelled', 0)} cancelled; per-recipient={snapshot.get('recipient_pending_items', 0)} across {snapshot.get('recipient_pending_recipients', 0)} recipients; bypass tasks={snapshot.get('bypass_tasks', 0)} pending={snapshot.get('bypass_pending_items', 0)} deferred={snapshot.get('bypass_deferred_items', 0)}",
        f"Scheduler: heap size={snapshot.get('heap_size', 0)}; queued recipients={snapshot.get('recipient_queued', 0)}; heap recipient handles={snapshot.get('recipient_heap_items', 0)}",
        f"Fanout: pending={snapshot.get('pending_fanout_total', 0)}; completed={snapshot.get('completed_fanout_total', 0)}; pending messages={snapshot.get('pending_messages', 0)}",
        f"Backlog: max recipient={snapshot.get('max_recipient_backlog', 0)}; oldest open message={_status_seconds(snapshot.get('oldest_open_message_age_seconds'))}",
        f"Delays: global wait={_status_seconds(snapshot.get('global_wait_seconds'))}; paused recipients={snapshot.get('paused_recipients', 0)}; max pause={_status_seconds(snapshot.get('max_pause_seconds'))}; wake tasks={snapshot.get('wake_tasks', 0)}",
        f"Rates: global={snapshot.get('rate_per_second')}/s; per recipient={snapshot.get('per_recipient_rate_per_second')}/s; active window={_status_seconds(active_window)}",
        f"Users: total={len(users)}; started active={active}; started inactive={inactive}; started no activity={never_active}; banned={sum(1 for user in users if user.is_banned)}; not started={sum(1 for user in users if not user.has_started and not user.is_banned)}; mods/admins={sum(1 for user in users if user.is_mod_or_admin)}",
        f"Cache: messages={len(store.messages)}; deliveries={len(store.deliveries)} ({delivery_deleted} deleted); whispers={len(store.whispers)} ({len(store.whisper_deliveries)} copies, {whisper_delivery_deleted} deleted); mod notes={len(store.mod_notes)} messages/{mod_note_copies} copies",
        f"Cache flags: deleted={message_deleted}; removed for mods={removed_for_mods}; system={system_messages}; urgent={urgent_messages}; mod whispers={mod_whispers}; confirmations={len(store.confirmations)}; retries={len(store.retries)}",
        f"Message types: {_status_counts(message_types)}",
        f"Delivery statuses: {_status_counts(snapshot.get('delivery_statuses', {}))}",
        f"Open content types: {_status_counts(snapshot.get('content_types', {}))}",
    ]
    if inflight:
        lines.append("Inflight: " + "; ".join(inflight[:5]))
    top_messages = snapshot.get("top_messages", [])[:8]
    if top_messages:
        lines.append("")
        lines.append("Top messages")
        lines.extend(_status_message_line(message) for message in top_messages)
    top_recipients = snapshot.get("top_recipients", [])[:8]
    if top_recipients:
        lines.append("")
        lines.append("Top recipients")
        lines.extend(_status_recipient_line(recipient) for recipient in top_recipients)
    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3850].rstrip() + "\n...truncated"
    return text


def _status_seconds(value) -> str:
    if value is None:
        return "n/a"
    return human_seconds(int(value))


def _status_counts(counts, *, limit: int = 8) -> str:
    items = [(str(key), int(value)) for key, value in dict(counts).items() if value]
    if not items:
        return "none"
    items.sort(key=lambda item: item[1], reverse=True)
    rendered = ", ".join(f"{key}={value}" for key, value in items[:limit])
    if len(items) > limit:
        rendered += f", +{len(items) - limit} more"
    return rendered


def _status_message_line(message: dict) -> str:
    flags = []
    if message.get("system"):
        flags.append("system")
    if message.get("urgent"):
        flags.append("urgent")
    if message.get("deleted"):
        flags.append("deleted")
    suffix = f" [{' '.join(flags)}]" if flags else ""
    return (
        f"- msg {message.get('message_id')} from {message.get('sender_id')} "
        f"{message.get('content_type')}: open={message.get('open')} "
        f"pending={message.get('pending')} done={message.get('completed')} "
        f"recipients={message.get('recipient_count')} age={_status_seconds(message.get('age_seconds'))}{suffix}"
    )


def _status_recipient_line(recipient: dict) -> str:
    flags = []
    if recipient.get("queued"):
        flags.append("queued")
    if recipient.get("heap_item"):
        flags.append("heap")
    if recipient.get("wake_task"):
        flags.append("wake")
    if recipient.get("mod"):
        flags.append("mod")
    if recipient.get("banned"):
        flags.append("banned")
    suffix = f" [{' '.join(flags)}]" if flags else ""
    return (
        f"- user {recipient.get('recipient_id')}: pending={recipient.get('pending')} "
        f"pause={_status_seconds(recipient.get('paused_seconds'))} "
        f"last={_status_seconds(recipient.get('last_activity_age_seconds'))}{suffix}"
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller, _ = await ensure_user(update, context)
    repo = get_repo(context)
    config = get_config(context)
    store = get_store(context)
    target = caller
    inspected_message_id = None
    reply = update.effective_message.reply_to_message if update.effective_message else None
    if caller and caller.is_mod_or_admin and reply:
        msg, delivery, error = await resolve_message_from_reply(update, context)
        if msg and msg.sender_id:
            target = repo.get_user(msg.sender_id)
            inspected_message_id = msg.id
        else:
            wdel = store.resolve_whisper_delivery(caller.telegram_id, reply.message_id)
            if wdel and store.whispers.get(wdel.whisper_id):
                whisper = store.whispers[wdel.whisper_id]
                target = repo.get_user(whisper.sender_id)
        if not target:
            await command_reply(update, context, error or "Message sender is not available.")
            return
    elif caller and caller.is_mod_or_admin and context.args:
        from forward_bot.identity import resolve_user_reference
        target = resolve_user_reference(repo, config, context.args[0], caller)
    elif caller and reply:
        msg, _, error = await resolve_message_from_reply(update, context)
        if msg:
            inspected_message_id = msg.id
            if msg.sender_id != caller.telegram_id:
                await command_reply(update, context, "Normal users cannot inspect others.")
                return
            target = caller
        else:
            wdel = store.resolve_whisper_delivery(caller.telegram_id, reply.message_id)
            if not wdel or not store.whispers.get(wdel.whisper_id):
                await command_reply(update, context, error or "Message is not in cache anymore.")
                return
            whisper = store.whispers[wdel.whisper_id]
            if caller.telegram_id not in {whisper.sender_id, whisper.target_id}:
                await command_reply(update, context, "Normal users cannot inspect others.")
                return
            target = repo.get_user(whisper.sender_id)
    elif context.args:
        await command_reply(update, context, "Normal users cannot inspect others.")
        return
    if not target:
        await command_reply(update, context, "User not found.")
        return
    lines = [
        f"User: {display_identity_html(target, config, viewer=caller)}",
        f"Role: {'admin' if target.is_admin else 'moderator' if target.is_moderator else 'user'}",
        f"Started: {target.has_started}",
        f"Banned: {target.is_banned}",
        f"Credits: {target.credits:.2f}",
        f"Warnings: {target.warning_count}",
        f"Votes received: +{target.upvotes_received} / -{target.downvotes_received}",
        f"Sign/tripcode: {target.sign_enabled}/{target.tripcode_enabled}",
        f"Daily tax: {tax_rate(config, target.credits) * 100:.2f}%",
        f"Loss rate: {loss_rate(config, target.credits) * 100:.2f}%",
    ]
    remove_window = int(config.get("vote_to_remove.user_remove_cooldown_seconds", 3600) or 3600)
    remove_cooldown = int(config.get("vote_to_remove.user_vote_cooldown_seconds", 300) or 300)
    lines.append(f"Remove votes in window: {store.recent_remove_votes_by_user(target.telegram_id, remove_window)}")
    left = store.latest_remove_vote_seconds_left(target.telegram_id, remove_cooldown)
    if left:
        lines.append(f"Remove-vote cooldown: {human_seconds(left)}")
    if target.active_cooldown_seconds:
        lines.append(f"Cooldown: {human_seconds(target.active_cooldown_seconds)} ({html_escape(target.cooldown_reason or 'cooldown')})")
    if caller and caller.is_mod_or_admin and inspected_message_id:
        meta = store.get_sender_snapshot(inspected_message_id)
        if meta:
            media_parts = []
            if meta.get("media_width") and meta.get("media_height"):
                media_parts.append(f"{meta['media_width']}x{meta['media_height']}")
            if meta.get("media_bytes"):
                media_parts.append(f"{meta['media_bytes']} bytes")
            if media_parts:
                lines.append("Media: " + ", ".join(media_parts))
            if meta.get("tag_reason"):
                lines.append(f"Tag reason: {html_escape(str(meta['tag_reason']))}")
    await command_reply(update, context, "\n".join(lines), parse_mode="HTML")


async def togglemod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_admin(update, context)
    if not caller:
        return
    target, error, _ = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, error or "User not found.")
        return
    target = get_repo(context).set_role(target.telegram_id, moderator=not target.is_moderator)
    await command_reply(update, context, f"Moderator for {display_identity_html(target, get_config(context), viewer=caller)}: {target.is_moderator}", parse_mode="HTML")
    try:
        await context.bot.send_message(target.telegram_id, f"Moderator status: {target.is_moderator}", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.togglemod_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass


async def mod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_mod(update, context, True)


async def unmod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_mod(update, context, False)


async def _set_mod(update: Update, context: ContextTypes.DEFAULT_TYPE, value: bool) -> None:
    caller = await _require_admin(update, context)
    if not caller:
        return
    target, error, _ = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, "Use /mod <user> or /unmod <user>, or reply to a message.")
        return
    target = get_repo(context).set_role(target.telegram_id, moderator=value)
    action = "promoted to moderator" if value else "removed as moderator"
    await command_reply(update, context, f"{display_identity_html(target, get_config(context), viewer=caller)} {action}.", parse_mode="HTML")
    try:
        await context.bot.send_message(target.telegram_id, f"You were {action}.", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.role_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_set(update, context, True)


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_set(update, context, False)


async def _ban_set(update: Update, context: ContextTypes.DEFAULT_TYPE, value: bool) -> None:
    caller = await _require_admin(update, context)
    if not caller:
        return
    target, error, _ = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, "Use /ban <user> or /unban <user>, or reply to a message.")
        return
    target = get_repo(context).set_role(target.telegram_id, banned=value)
    action = "Banned" if value else "Unbanned"
    await command_reply(update, context, f"{action} {display_identity_html(target, get_config(context), viewer=caller)}", parse_mode="HTML")
    try:
        await context.bot.send_message(target.telegram_id, "You are banned." if value else "You are unbanned.", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.ban_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    target, error, rest = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, "Use /warn <user> [message], or reply with /warn [message].")
        return
    text = rest or "Warned by moderator"
    count = get_repo(context).increment_warning(target.telegram_id)
    suffix = "~ mods"
    try:
        await context.bot.send_message(target.telegram_id, f"{text}\n\n{suffix}", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.warn_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass
    await command_reply(update, context, f"Warning issued. Total warnings: {count}")


async def cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    target, error, rest = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, "Use /cooldown <user> [duration] [reason], or reply with /cooldown [duration] [reason]. Duration supports 30s, 10m, 2h, 1d.")
        return
    default = int(get_config(context).get("moderation.default_cooldown_seconds", 1800) or 1800)
    seconds, reason = parse_duration_seconds(rest, default)
    reason = reason or "cooldown"
    get_repo(context).set_cooldown(target.telegram_id, seconds, reason, caller.telegram_id, stack=True)
    try:
        await context.bot.send_message(target.telegram_id, f"Cooldown: {human_seconds(seconds)}. Reason: {reason}", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.cooldown_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass
    await command_reply(update, context, f"Cooldown applied for {human_seconds(seconds)}.")


async def uncooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    target, error, _ = await _resolve_target(update, context, caller)
    if not target:
        await command_reply(update, context, "Use /uncooldown <user>, or reply with /uncooldown.")
        return
    get_repo(context).clear_cooldown(target.telegram_id)
    try:
        await context.bot.send_message(target.telegram_id, "Cooldown cleared.", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
    except TelegramError as exc:
        log_telegram_error(LOGGER, "mod.uncooldown_notify", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=get_repo(context), user_id=target.telegram_id)
        pass
    await command_reply(update, context, "Cooldown cleared.")


async def moderated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    repo = get_repo(context)
    config = get_config(context)
    lines = ["Banned:"]
    for user in repo.list_users():
        if user.is_banned:
            lines.append(f"- {display_identity_html(user, config, viewer=caller)}")
    lines.append("\nAdmins/moderators:")
    for user in repo.list_users():
        if user.is_admin or user.is_moderator:
            lines.append(f"- {display_identity_html(user, config, viewer=caller)} ({'admin' if user.is_admin else 'mod'})")
    lines.append("\nActive cooldowns:")
    for user, left, reason in repo.list_active_cooldowns():
        lines.append(f"- {display_identity_html(user, config, viewer=caller)}: {human_seconds(left)} {html_escape(reason)}")
    await update.effective_message.reply_html("\n".join(lines))


async def purgebanned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    repo = get_repo(context)
    store = get_store(context)
    count = 0
    for msg in list(store.messages.values()):
        sender = repo.get_user(msg.sender_id) if msg.sender_id else None
        if sender and sender.is_banned and (not msg.deleted or not msg.removed_for_mods):
            if not msg.deleted:
                await remove_message(context.bot, repo, store, get_config(context), msg.id, reason="purged banned sender", remove_for_mods=False, notify_sender=False, send_note=False)
            if not msg.removed_for_mods:
                await remove_message(context.bot, repo, store, get_config(context), msg.id, reason="purged banned sender", remove_for_mods=True, notify_sender=False, send_note=False)
            count += 1
    await update.effective_message.reply_text(f"Purged {count} cached messages.")


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    msg, _, error = await resolve_message_from_reply(update, context)
    if not msg:
        wdel = None
        if update.effective_message.reply_to_message:
            wdel = get_store(context).resolve_whisper_delivery(caller.telegram_id, update.effective_message.reply_to_message.message_id)
        if wdel:
            count = await remove_whisper(context.bot, get_repo(context), get_store(context), get_config(context), wdel.whisper_id, "deleted by moderator")
            await command_reply(update, context, f"Deleted ({count} copies).")
            return
        await command_reply(update, context, error or "Message is not in cache anymore.")
        return
    if caller.is_admin:
        await mark_for_moderation_action(context.bot, get_repo(context), get_store(context), get_config(context), msg.id)
        count = await remove_message(context.bot, get_repo(context), get_store(context), get_config(context), msg.id, reason="deleted by moderator", remove_for_mods=False)
        await command_reply(update, context, f"Deleted ({count} copies).")
    else:
        await mark_for_moderation_action(context.bot, get_repo(context), get_store(context), get_config(context), msg.id)
        count = await remove_message(context.bot, get_repo(context), get_store(context), get_config(context), msg.id, reason="deleted by moderator", remove_for_mods=False)
        await command_reply(update, context, f"Deleted ({count} copies).")


async def blocksticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if not caller:
        return
    reply = update.effective_message.reply_to_message if update.effective_message else None
    sticker = reply.sticker if reply else None
    if not sticker or not sticker.set_name:
        await command_reply(update, context, "Reply to a sticker with a sticker set.")
        return
    reason = args_text(context) or "blocked by moderator"
    get_repo(context).block_sticker_set(sticker.set_name, caller.telegram_id, reason)
    await command_reply(update, context, f"Sticker pack blocked: {sticker.set_name}")


async def modsay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller = await _require_mod(update, context)
    if caller:
        await _broadcast(update, context, suffix="~ mods", admin_only=False)


async def adminsay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_admin(update, context):
        await _broadcast(update, context, suffix="~ admin", admin_only=True)


async def _broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, *, suffix: str, admin_only: bool) -> None:
    text = args_text(context)
    if not text:
        await update.effective_message.reply_text("Text required.")
        return
    repo = get_repo(context)
    store = get_store(context)
    sender_id = update.effective_user.id
    tm = store.add_message(sender_id=sender_id, content_type="text", text=f"{text}\n\n{suffix}", source_chat_id=update.effective_chat.id, source_message_id=update.effective_message.message_id, is_system=True, urgent=True)
    recipients = [u for u in repo.eligible_recipients(sender_id, include_sender=False)]
    context.application.bot_data["queue"].enqueue_message(tm, recipients)
    await update.effective_message.reply_text("Message sent.")


async def reload_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    old: Config = get_config(context)
    new = old.reload()
    context.application.bot_data["config_ref"]["config"] = new
    repo = get_repo(context)
    repo.sync_admin_ids(new.get("bot.admin_ids", []) or [])
    context.application.bot_data["tagger"].refresh(new)
    context.application.bot_data["ai"].update_config(new.section("ai"))
    context.application.bot_data["rate_limiter"].update_config(int(new.get("rate_limits.message_send_limit", 8) or 8), int(new.get("rate_limits.window_seconds", 30) or 30))
    context.application.bot_data["queue"].update_config(new)
    await update.effective_message.reply_text("Config reloaded.")
