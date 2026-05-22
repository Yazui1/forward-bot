from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import logging
import html

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from forward_bot.cache.state import CachedSenderMetadata, SenderMetadataCache
from forward_bot.crypto.obfuscation import temporal_id
from forward_bot.features.credits import interpolate_loss_rate, interpolate_tax_rate
from forward_bot.features.tombstones import remove_message_with_tombstones, tombstone
from forward_bot.utils import as_utc
from forward_bot.commands.help_registry import register_command
from forward_bot.config import Config
from forward_bot.messages import Messages as Msg

logger = logging.getLogger(__name__)


def register_mod_commands(app: Any, repo: Any, cfg: dict[str, Any], sender_cache: SenderMetadataCache) -> None:
    def add(cmd: str, handler: Any, desc: Any) -> None:
        register_command(
            app, f"/{cmd}", CommandHandler(cmd, handler), "Mod", desc)

    add("info", _info(repo, cfg, sender_cache),
        lambda cfg: "moderator/admin sender lookup (reply mode)")
    add("togglemod", _toggle_mod(repo),
        lambda cfg: "promote/demote moderator (admin only)")
    add("ban", _ban(repo), lambda cfg: "ban user (admin only)")
    add("unban", _unban(repo), lambda cfg: "unban user (admin only)")
    add("warn", _warn(repo), lambda cfg: "warn user (mod/admin)")
    add("cooldown", _cooldown(repo, cfg),
        lambda cfg: "cooldown user (mod/admin)")
    add("uncooldown", _uncooldown(repo),
        lambda cfg: "remove cooldown (mod/admin)")
    add("purgebanned", _purge_banned(repo),
        lambda cfg: "show banned users and cooldown history (admin only)")
    add("moderated", _moderated(repo, cfg),
        lambda cfg: "show moderation info (mod/admin)")
    add("delete", _delete(repo),
        lambda cfg: "delete a replied message (admin immediate, mod confirmation)")
    add("modsay", _modsay(repo), lambda cfg: "broadcast to active users (mod/admin)")
    add("adminsay", _adminsay(repo),
        lambda cfg: "broadcast to active users (admin only)")
    add("reload", _reload(repo), lambda cfg: "reload config.yml (admin only)")


def _is_mod_or_admin(user: Any) -> bool:
    return bool(user and (user.is_moderator or user.is_admin))


async def _caller(repo: Any, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
    if update.effective_user is None:
        return None
    cfg = context.application.bot_data.get("cfg", {})
    return await repo.get_or_create_user(
        update.effective_user.id,
        update.effective_user.username,
        set(int(x) for x in cfg.get("bot", {}).get("admin_ids", [])),
        starting_credits=float(
            cfg.get("credits", {}).get("starting_balance", 20.0)),
    )


async def _self_info_text(repo: Any, cfg: dict[str, Any], caller: Any) -> str:
    tax = interpolate_tax_rate(
        cfg["credits"]["tax_ramp"], caller.credits) * 100.0
    loss = interpolate_loss_rate(
        cfg["loss_rate"]["schedule"], caller.credits) * 100.0
    upvotes, downvotes = await repo.get_received_vote_counts(caller.telegram_id)
    remove_count = await repo.count_user_remove_votes_in_window(
        caller.telegram_id,
        int(cfg["vote_to_remove"]["user_remove_cooldown_seconds"]),
    )
    cd = await repo.get_active_cooldown(caller.telegram_id)
    cooldown_lines = []
    if cd is not None:
        cooldown_lines.append(
            f"Cooldown remaining: {_cooldown_remaining_text(cd)}")
    return "\n".join(
        [
            f"Temporal ID: {temporal_id(caller.telegram_id, cfg['bot']['global_salt'])}",
            *(["Role: admin"] if caller.is_admin else []),
            f"Credits: {caller.credits:.2f}",
            f"Votes received: +{upvotes} / -{downvotes}",
            f"Sign enabled: {caller.sign_enabled}",
            f"Tripcode enabled: {caller.tripcode_enabled}",
            f"Daily tax rate: {tax:.2f}%",
            f"Loss rate: {loss:.2f}%",
            f"Remove cooldown count: {remove_count}/{int(cfg['vote_to_remove']['user_remove_limit'])}",
            *cooldown_lines,
        ]
    )


async def _replace_text_with_tombstone(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, reason: str) -> None:
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=tombstone(reason),
            parse_mode="HTML",
        )
        return
    except Exception:
        pass
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=chat_id, text=tombstone(reason), parse_mode="HTML")
    except Exception:
        pass


async def _delete_whisper(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    whisper: dict[str, Any],
    reason: str,
) -> None:
    whisper_id = int(whisper["id"])
    sender_id = int(whisper["sender_id"])
    label = "Modwhisper" if bool(whisper.get("is_modwhisper")) else "Whisper"
    base = f"<i><b>{label}:</b></i> {html.escape(str(whisper.get('text_content') or ''))}"
    mod_text = f"{base}\n\n<b><i>This message was removed and is pending moderation action</i></b>"
    for delivery in await repo.list_whisper_deliveries(whisper_id):
        recipient_id = int(delivery["recipient_id"])
        telegram_message_id = int(delivery["telegram_message_id"])
        recipient = await repo.get_user(recipient_id)
        is_mod = bool(recipient and (
            recipient.is_moderator or recipient.is_admin))
        if recipient_id == sender_id:
            try:
                await context.bot.send_message(
                    chat_id=recipient_id,
                    text=Msg.whisper_removed_pending(reason),
                    reply_to_message_id=telegram_message_id,
                )
            except Exception:
                pass
            continue
        if is_mod:
            try:
                await context.bot.edit_message_text(
                    chat_id=recipient_id,
                    message_id=telegram_message_id,
                    text=mod_text,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            continue
        await _replace_text_with_tombstone(context, recipient_id, telegram_message_id, reason)
    await repo.add_audit_event("whisper_removed", target_user_id=sender_id, message_id=-whisper_id, details=reason)


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


def _parse_duration_to_seconds(token: str | None, fallback_seconds: int) -> int:
    if not token:
        return fallback_seconds
    t = token.strip().lower()
    num = ""
    unit = ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            unit += ch
    if not num:
        return fallback_seconds
    value = float(num)
    if unit in ("s", "sec", "secs", "second", "seconds", ""):
        return int(value)
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return int(value * 60)
    if unit in ("h", "hr", "hour", "hours"):
        return int(value * 3600)
    if unit in ("d", "day", "days"):
        return int(value * 86400)
    return fallback_seconds


def _resolve_target_from_reply_or_arg(repo: Any, update: Update, arg0: str | None) -> Any:
    async def inner() -> int | None:
        if update.message and update.message.reply_to_message and update.effective_user:
            lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
            if lookup:
                _, sid = lookup
                return sid
            whisper = await repo.whisper_context_by_reply(
                update.effective_user.id,
                update.message.reply_to_message.message_id,
            )
            if whisper is not None:
                return int(whisper["sender_id"])
        if arg0:
            if arg0.startswith("@"):
                user = await repo.get_user_by_username(arg0[1:])
                return None if user is None else user.telegram_id
            try:
                return int(arg0)
            except ValueError:
                return None
        return None

    return inner()


def _identity_for_viewer(user: Any, viewer: Any, salt: str) -> str:
    if viewer.is_admin:
        return f"@{user.username}" if user.username else str(user.telegram_id)
    if user.tripcode_name and user.tripcode_hash:
        return f"{user.tripcode_name} !{str(user.tripcode_hash)[:6]}"
    return temporal_id(user.telegram_id, salt)


def _role(user: Any) -> str:
    return "admin" if user.is_admin else "moderator" if user.is_moderator else "user"


def _info(repo: Any, cfg: dict[str, Any], sender_cache: SenderMetadataCache):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if caller is None:
            return
        if not _is_mod_or_admin(caller) and context.args:
            await update.message.reply_text(Msg.INFO_SELF_ONLY)
            return
        if not _is_mod_or_admin(caller) and update.message.reply_to_message:
            own = await repo.message_by_source(caller.telegram_id, update.message.reply_to_message.message_id)
            if own is None:
                lookup = await repo.sender_by_delivery(caller.telegram_id, update.message.reply_to_message.message_id)
                if lookup is None:
                    await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                    return
                if lookup[1] != caller.telegram_id:
                    await update.message.reply_text(Msg.INFO_SELF_ONLY)
                    return
        if not _is_mod_or_admin(caller):
            # Normal users can inspect only themselves.
            await update.message.reply_text(await _self_info_text(repo, cfg, caller))
            return

        if update.message.reply_to_message:
            lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
            if lookup is None:
                whisper_sender = await repo.whisper_sender_by_reply(
                    update.effective_user.id,
                    update.message.reply_to_message.message_id,
                )
                if whisper_sender is not None:
                    lookup = (-int(update.message.reply_to_message.message_id),
                              whisper_sender)
            if lookup is None:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            message_id, sender_id = lookup
            cached = sender_cache.get(message_id)
            sender_user = await repo.get_user(sender_id)
            if cached is None and sender_user is not None:
                cached = CachedSenderMetadata(
                    sender_id=sender_id,
                    username=sender_user.username,
                    temporal_id=temporal_id(
                        sender_id, cfg["bot"]["global_salt"]),
                    role="admin" if sender_user.is_admin else "moderator" if sender_user.is_moderator else "user",
                    credits=sender_user.credits,
                    cached_at=0.0,
                )
                sender_cache.set(message_id, cached)
            if cached is None:
                await update.message.reply_text(Msg.SENDER_NOT_FOUND)
                return
            warn_count = await repo.warning_count(cached.sender_id)
            upvotes, downvotes = await repo.get_received_vote_counts(cached.sender_id)
            cd = await repo.get_active_cooldown(cached.sender_id)
            lines = [
                f"Temporal ID: {cached.temporal_id}",
                f"Credits: {cached.credits:.2f}",
                f"Warnings: {warn_count}",
                f"Votes received: +{upvotes} / -{downvotes}",
            ]
            if caller.is_admin:
                lines.insert(1, f"Role: {cached.role}")
            if sender_user is not None:
                lines.append(f"Sign enabled: {sender_user.sign_enabled}")
                lines.append(
                    f"Tripcode enabled: {sender_user.tripcode_enabled}")
                if caller.is_admin and sender_user.tripcode_name and sender_user.tripcode_hash:
                    lines.append(
                        f"Tripcode: {sender_user.tripcode_name} !{str(sender_user.tripcode_hash)[:6]}")
            tax = interpolate_tax_rate(
                cfg["credits"]["tax_ramp"], cached.credits) * 100.0
            lines.append(f"Daily tax rate: {tax:.2f}%")
            loss = interpolate_loss_rate(
                cfg["loss_rate"]["schedule"], cached.credits) * 100.0
            lines.append(f"Loss rate: {loss:.2f}%")
            remove_count = await repo.count_user_remove_votes_in_window(
                cached.sender_id,
                int(cfg["vote_to_remove"]["user_remove_cooldown_seconds"]),
            )
            lines.append(
                f"Remove cooldown count: {remove_count}/{int(cfg['vote_to_remove']['user_remove_limit'])}"
            )
            last_remove = await repo.user_last_remove_vote_at(cached.sender_id)
            if last_remove:
                try:
                    lv = as_utc(last_remove)
                    rem = int(cfg["vote_to_remove"]["user_vote_cooldown_seconds"]) - int(
                        (datetime.now(timezone.utc) - lv).total_seconds()
                    )
                    lines.append(f"Next remove-vote cooldown: {max(0, rem)}s")
                except ValueError:
                    pass
            if cd is not None:
                lines.append(
                    f"Cooldown remaining: {_cooldown_remaining_text(cd)}")
            if caller.is_admin:
                lines.append(f"Telegram ID: {cached.sender_id}")
                lines.append(
                    f"Username: @{cached.username}" if cached.username else "Username: <none>")
            await update.message.reply_text("\n".join(lines))
            return

        if context.args:
            arg = context.args[0].strip()
            if arg.startswith("@") and caller.is_admin:
                target = await repo.get_user_by_username(arg[1:])
            else:
                target = None
                for u in await repo.list_users():
                    if temporal_id(u.telegram_id, cfg["bot"]["global_salt"]) == arg.upper():
                        target = u
                        break
            if target is None:
                await update.message.reply_text(Msg.USER_NOT_FOUND)
                return
            tax = interpolate_tax_rate(
                cfg["credits"]["tax_ramp"], target.credits) * 100.0
            warn_count = await repo.warning_count(target.telegram_id)
            upvotes, downvotes = await repo.get_received_vote_counts(target.telegram_id)
            cd = await repo.get_active_cooldown(target.telegram_id)
            lines = [
                f"Temporal ID: {temporal_id(target.telegram_id, cfg['bot']['global_salt'])}",
                f"Credits: {target.credits:.2f}",
                f"Warnings: {warn_count}",
                f"Votes received: +{upvotes} / -{downvotes}",
                f"Sign enabled: {target.sign_enabled}",
                f"Tripcode enabled: {target.tripcode_enabled}",
                f"Daily tax rate: {tax:.2f}%",
            ]
            if caller.is_admin:
                lines.insert(1, f"Role: {_role(target)}")
            loss = interpolate_loss_rate(
                cfg["loss_rate"]["schedule"], target.credits) * 100.0
            lines.append(f"Loss rate: {loss:.2f}%")
            remove_count = await repo.count_user_remove_votes_in_window(
                target.telegram_id,
                int(cfg["vote_to_remove"]["user_remove_cooldown_seconds"]),
            )
            lines.append(
                f"Remove cooldown count: {remove_count}/{int(cfg['vote_to_remove']['user_remove_limit'])}"
            )
            last_remove = await repo.user_last_remove_vote_at(target.telegram_id)
            if last_remove:
                try:
                    lv = as_utc(last_remove)
                    rem = int(cfg["vote_to_remove"]["user_vote_cooldown_seconds"]) - int(
                        (datetime.now(timezone.utc) - lv).total_seconds()
                    )
                    lines.append(f"Next remove-vote cooldown: {max(0, rem)}s")
                except ValueError:
                    pass
            if cd is not None:
                lines.append(
                    f"Cooldown remaining: {_cooldown_remaining_text(cd)}")
            if caller.is_admin:
                lines.append(f"Telegram ID: {target.telegram_id}")
                lines.append(
                    f"Username: @{target.username}" if target.username else "Username: <none>")
                if target.tripcode_name and target.tripcode_hash:
                    lines.append(
                        f"Tripcode: {target.tripcode_name} !{str(target.tripcode_hash)[:6]}")
            await update.message.reply_text("\n".join(lines))
            return

        await update.message.reply_text(await _self_info_text(repo, cfg, caller))

    return handler


def _reload(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if caller is None or not caller.is_admin:
            await update.message.reply_text(Msg.ADMIN_ONLY)
            return
        config_path = str(context.application.bot_data.get("config_path", "config.yml"))
        try:
            new_cfg = Config(config_path).data
        except Exception as exc:
            await update.message.reply_text(Msg.reload_failed(exc))
            return
        cfg_ref = context.application.bot_data.get("cfg")
        if isinstance(cfg_ref, dict):
            cfg_ref.clear()
            cfg_ref.update(new_cfg)
        else:
            context.application.bot_data["cfg"] = new_cfg
        await repo.sync_admin_ids(set(int(x) for x in new_cfg["bot"].get("admin_ids", [])))
        tagger = context.application.bot_data.get("tagger")
        if tagger is not None:
            tagger.blocked_terms = [x.lower()
                                    for x in new_cfg["tagging"]["blocked_terms"]]
            tagger.questionable_terms = [
                x.lower() for x in new_cfg["tagging"]["questionable_terms"]]
            tagger.potentially_unwanted_terms = [
                x.lower() for x in new_cfg["tagging"].get("potentially_unwanted_terms", [])]
        ai_classifier = context.application.bot_data.get("ai_classifier")
        if ai_classifier is not None:
            ai_classifier.update_config(new_cfg)
        rate_limiter = context.application.bot_data.get("rate_limiter")
        if rate_limiter is not None:
            rate_limiter.limit = int(
                new_cfg["rate_limits"]["message_send_limit"])
            rate_limiter.window_seconds = int(
                new_cfg["rate_limits"]["window_seconds"])
        queue = context.application.bot_data.get("queue")
        if queue is not None:
            queue.update_config(new_cfg)
        await update.message.reply_text(Msg.CONFIG_RELOADED)
        logger.info("Config reloaded by admin_id=%s", caller.telegram_id)

    return handler


def _toggle_mod(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if caller is None or not caller.is_admin:
            await update.message.reply_text(Msg.ADMIN_ONLY)
            return
        target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0] if context.args else None)
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_TOGGLEMOD)
            return
        target = await repo.get_user(target_id)
        if target is None:
            await update.message.reply_text(Msg.TARGET_NOT_FOUND)
            return
        new_state = not target.is_moderator
        await repo.set_moderator(target_id, new_state)
        await update.message.reply_text(Msg.moderator_toggled(new_state, target_id))
        try:
            await context.bot.send_message(chat_id=target_id, text=Msg.moderator_status_changed(new_state))
        except Exception:
            pass
        logger.info("Moderator toggle target_id=%s new_state=%s by_admin=%s",
                    target_id, new_state, caller.telegram_id)

    return handler


def _ban(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if caller is None or not caller.is_admin:
            await update.message.reply_text(Msg.ADMIN_ONLY)
            return
        target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0] if context.args else None)
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_BAN)
            return
        await repo.set_banned(target_id, True)
        try:
            await context.bot.send_message(chat_id=target_id, text=Msg.BANNED_NOTIFY)
        except Exception:
            pass
        await update.message.reply_text(Msg.banned_target(target_id))

    return handler


def _unban(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if caller is None or not caller.is_admin:
            await update.message.reply_text(Msg.ADMIN_ONLY)
            return
        target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0] if context.args else None)
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_UNBAN)
            return
        await repo.set_banned(target_id, False)
        try:
            await context.bot.send_message(chat_id=target_id, text=Msg.UNBANNED_NOTIFY)
        except Exception:
            pass
        await update.message.reply_text(Msg.unbanned_target(target_id))

    return handler


def _warn(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0] if context.args else None)
        warned_message_id = None
        warned_whisper_id = None
        if update.message.reply_to_message:
            lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
            if lookup is None:
                whisper = await repo.whisper_context_by_reply(
                    update.effective_user.id,
                    update.message.reply_to_message.message_id,
                )
                if whisper is None:
                    await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                    return
                warned_whisper_id = int(whisper["id"])
            else:
                warned_message_id, _ = lookup
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_WARN)
            return
        if update.message.reply_to_message:
            msg = " ".join(context.args).strip() or "Warned by moderator"
        else:
            msg = " ".join(context.args[1:]).strip() if len(
                context.args) > 1 else "Warned by moderator"
        await repo.add_warning(target_id, caller.telegram_id, msg)
        try:
            suffix = "~ admin" if caller.is_admin else "~ mods"
            reply_to = None
            if warned_message_id:
                reply_to = await repo.delivery_message_for_recipient(warned_message_id, target_id)
            elif warned_whisper_id:
                reply_to = await repo.whisper_delivery_message_id(warned_whisper_id, target_id)
            await context.bot.send_message(
                chat_id=target_id,
                text=Msg.warning(html.escape(msg), suffix),
                parse_mode="HTML",
                reply_to_message_id=reply_to,
            )
        except Exception:
            pass
        await update.message.reply_text(Msg.WARNING_ISSUED, reply_to_message_id=update.message.reply_to_message.message_id if update.message.reply_to_message else None)

    return handler


def _cooldown(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        arg0 = context.args[0] if context.args else None
        target_id = await _resolve_target_from_reply_or_arg(repo, update, arg0 if arg0 and arg0.startswith("@") else None)
        duration_token = None
        reason = "cooldown"
        if update.message.reply_to_message:
            duration_token = context.args[0] if context.args else None
            if len(context.args) > 1:
                reason = " ".join(context.args[1:])
        else:
            if target_id is None and context.args:
                target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0])
            if len(context.args) >= 2:
                duration_token = context.args[1]
            if len(context.args) >= 3:
                reason = " ".join(context.args[2:])
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_COOLDOWN)
            return
        default_seconds = int(cfg.get("moderation", {}).get(
            "default_cooldown_seconds", 1800))
        seconds = _parse_duration_to_seconds(duration_token, default_seconds)
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await repo.set_cooldown(target_id, until.isoformat(), reason, caller.telegram_id)
        cooldown_reply_to = None
        if update.message.reply_to_message:
            lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
            if lookup:
                msg_id, _ = lookup
                cooldown_reply_to = await repo.delivery_message_for_recipient(msg_id, target_id)
            else:
                whisper = await repo.whisper_context_by_reply(
                    update.effective_user.id,
                    update.message.reply_to_message.message_id,
                )
                if whisper is None:
                    await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                    return
                cooldown_reply_to = await repo.whisper_delivery_message_id(int(whisper["id"]), target_id)
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=Msg.cooldown_received(seconds, reason),
                reply_to_message_id=cooldown_reply_to,
            )
        except Exception:
            pass
        await update.message.reply_text(Msg.cooldown_set(target_id, seconds))

    return handler


def _uncooldown(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        target_id = await _resolve_target_from_reply_or_arg(repo, update, context.args[0] if context.args else None)
        if target_id is None:
            if update.message.reply_to_message:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await update.message.reply_text(Msg.USAGE_UNCOOLDOWN)
            return
        await repo.clear_cooldown(target_id)
        try:
            await context.bot.send_message(chat_id=target_id, text=Msg.UNCOOLDOWN_NOTIFY)
        except Exception:
            pass
        await update.message.reply_text(Msg.cooldown_removed(target_id))

    return handler


def _purge_banned(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        banned_users = await repo.list_banned_users()
        removed = 0
        for u in banned_users:
            msgs = await repo.list_messages_by_sender(u.telegram_id)
            for m in msgs:
                await remove_message_with_tombstones(
                    context,
                    repo,
                    context.application.bot_data["cfg"],
                    int(m["id"]),
                    u.telegram_id,
                    "purged banned user",
                )
                removed += 1
        await update.message.reply_text(Msg.purged_banned(removed))

    return handler


def _moderated(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        banned = await repo.list_banned_users()
        active_cooldowns = await repo.list_active_cooldowns()
        privileged = await repo.list_mod_and_admin_users()
        admins = [u for u in privileged if u.is_admin]
        mods = [u for u in privileged if u.is_moderator and not u.is_admin]
        lines = ["Banned users:"]
        lines.extend(
            [f"- {_identity_for_viewer(u, caller, cfg['bot']['global_salt'])}" for u in banned] or ["- none"])
        lines.append("")
        lines.append("Admins:")
        lines.extend(
            [f"- {_identity_for_viewer(u, caller, cfg['bot']['global_salt'])}" for u in admins] or ["- none"])
        lines.append("")
        lines.append("Moderators:")
        lines.extend(
            [f"- {_identity_for_viewer(u, caller, cfg['bot']['global_salt'])}" for u in mods] or ["- none"])
        lines.append("")
        lines.append("Currently cooled down:")
        active_count = 0
        for r in active_cooldowns:
            user = await repo.get_user(int(r["user_id"]))
            ident = _identity_for_viewer(user, caller, cfg["bot"]["global_salt"]) if user else temporal_id(
                int(r["user_id"]), cfg["bot"]["global_salt"])
            lines.append(
                f"- {ident}: {_cooldown_remaining_text(r)} remaining, reason={r['reason'] or '-'}")
            active_count += 1
        if active_count == 0:
            lines.append("- none")
        await update.message.reply_text("\n".join(lines))

    return handler


def _delete(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        if update.message.reply_to_message is None:
            await update.message.reply_text(Msg.USAGE_DELETE)
            return
        lookup = await repo.sender_by_delivery(
            update.effective_user.id,
            update.message.reply_to_message.message_id,
        )
        if lookup is None:
            whisper = await repo.whisper_context_by_reply(
                update.effective_user.id,
                update.message.reply_to_message.message_id,
            )
            if whisper is None:
                await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return
            await _delete_whisper(
                context,
                repo,
                whisper,
                "deleted by admin" if caller.is_admin else "deleted by moderator",
            )
            await update.message.reply_text(Msg.DELETE_WHISPER_FOR_USERS)
            return
        message_id, sender_id = lookup
        if await repo.get_message(message_id) is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        await remove_message_with_tombstones(
            context,
            repo,
            context.application.bot_data["cfg"],
            message_id,
            sender_id,
            "deleted by admin" if caller.is_admin else "deleted by moderator",
        )
        await update.message.reply_text(Msg.DELETE_FOR_USERS)

    return handler


def _modsay(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None or not context.args:
            if update.message:
                await update.message.reply_text(Msg.USAGE_MODSAY)
            return
        caller = await _caller(repo, update, context)
        if not _is_mod_or_admin(caller):
            await update.message.reply_text(Msg.MOD_ONLY)
            return
        text = f"{html.escape(' '.join(context.args))} <b><i>~ mods</i></b>"
        mods = [u for u in await repo.list_eligible_recipients(-1) if u.telegram_id != caller.telegram_id]
        queue = context.application.bot_data["queue"]
        msg_id = await repo.create_message(caller.telegram_id, "text", text, None, None, parse_mode="HTML")
        await repo.set_message_tag(msg_id, "OK", None)
        await queue.enqueue_batch(msg_id, caller.telegram_id, mods, "text", text, None, None, is_system=True, parse_mode="HTML")
        await update.message.reply_text(Msg.MESSAGE_SENT)

    return handler


def _adminsay(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None or not context.args:
            if update.message:
                await update.message.reply_text(Msg.USAGE_ADMINSAY)
            return
        caller = await _caller(repo, update, context)
        if caller is None or not caller.is_admin:
            await update.message.reply_text(Msg.ADMIN_ONLY)
            return
        text = f"{html.escape(' '.join(context.args))} <b><i>~ admin</i></b>"
        users = [u for u in await repo.list_eligible_recipients(-1) if u.telegram_id != caller.telegram_id]
        queue = context.application.bot_data["queue"]
        msg_id = await repo.create_message(caller.telegram_id, "text", text, None, None, parse_mode="HTML")
        await repo.set_message_tag(msg_id, "OK", None)
        await queue.enqueue_batch(msg_id, caller.telegram_id, users, "text", text, None, None, is_system=True, parse_mode="HTML")
        await update.message.reply_text(Msg.MESSAGE_SENT)

    return handler
