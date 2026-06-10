from __future__ import annotations

import math
import random
import secrets
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes

from forward_bot.crypto import hash_tripcode, temporal_id
from forward_bot.features.credits import adjust_credits_with_daily_limit, apply_negative_credit_cooldown, interpolate_loss_rate, interpolate_tax_rate, round_credit
from forward_bot.features.remove_votes import check_remove_vote_allowed
from forward_bot.features.tagging import TELEGRAM_INVITE_RE
from forward_bot.features.tombstones import remove_message_with_tombstones
from forward_bot.utils import as_utc, resolve_reply_target, resolve_user_reference, safe_reply_text, safe_send_message
from forward_bot.commands.help_registry import register_command
from forward_bot.messages import Messages as Msg

logger = logging.getLogger(__name__)


def register_user_commands(app: Any, repo: Any, cfg: dict[str, Any]) -> None:
    def add(cmd: str, handler: Any, desc: Any) -> None:
        register_command(
            app, f"/{cmd}", CommandHandler(cmd, handler), "User", desc)

    add("start", _start(repo, cfg), lambda cfg: "start receiving messages")
    add("stop", _stop(repo), lambda cfg: "stop receiving messages")
    add("help", _help(), lambda cfg: "show this help message")
    add("about", _about(repo, cfg), lambda cfg: "show about message")
    add("toggleconfirmation", _toggle_confirmation(repo),
        lambda cfg: "toggle questionable confirmation")
    add("togglevotebutton", _toggle_vote_button(repo),
        lambda cfg: "show/hide vote buttons on messages you receive")
    add("togglepotentiallyunwanted", _toggle_potentially_unwanted(repo),
        lambda cfg: "hide/show potentially unwanted messages")
    add("toggledups", _toggle_duplicates(repo),
        lambda cfg: "hide/show duplicate media you already saw")
    add("togglesign", _toggle_sign(repo, cfg),
        lambda cfg: "enable/disable persistent signed style")
    add("toggletripcode", _toggle_tripcode(repo, cfg),
        lambda cfg: "enable/disable persistent tripcode")
    add("block", _block(repo), lambda cfg: "block sender from replied message")
    add("unblock", _unblock(repo),
        lambda cfg: "unblock your most recently blocked sender")
    add("credit", _credit(repo, cfg), lambda cfg: "transfer credits")
    add("creditstats", _creditstats(repo, cfg),
        lambda cfg: "credit leaderboard and stats")
    add("gamble", _gamble(repo, cfg), lambda cfg: "50/50 credit gamble")
    add("t", _tripcode_send(repo, cfg), lambda cfg: "send tripcoded message")
    add("settripcode", _set_tripcode(repo, cfg),
        lambda cfg: "set tripcode (<name#secret>)")
    add("unsettripcode", _unset_tripcode(repo),
        lambda cfg: "clear your configured tripcode")
    add("s", _signed_send(repo, cfg), lambda cfg: "send signed message")
    add("sendinvite", _send_invite_message(repo, cfg),
        lambda cfg: "send a Telegram invite with required description")
    add("sauce", _sauce(repo, cfg),
        lambda cfg: "reverse image search replied media with SauceNAO")
    add("invite", _invite(repo, cfg), lambda cfg: "show your invite link")
    add("unsend", _unsend(repo, cfg),
        lambda cfg: f"remove your latest sent message (cost {float(cfg['credits']['unsend_cost']):.2f})")
    add("deletevote", _delete_vote(repo, cfg),
        lambda cfg: "vote to remove a replied message")
    add("reactions", _noop_help(),
        lambda cfg: f"react 👍, 🔥 or ❤️ to upvote (cost {float(cfg['credits']['upvote_cost']):.2f}) or 👎 to downvote (starts at {float(cfg['credits']['downvote_start_cost']):.2f})")
    add("w", _w(repo, cfg),
        lambda cfg: f"send whisper (cost {float(cfg['credits']['whisper_cost']):.2f}, unlock {float(cfg['credits']['whisper_unlock_credits']):.2f})")
    add("wmods", _whisper_mod(repo),
        lambda cfg: "whisper all moderators")
    add("fight", _fight(repo, cfg),
        lambda cfg: f"request fight (fee {float(cfg['fights']['initiation_fee_percent']) * 100.0:.0f}% of stake, min {float(cfg['fights']['initiation_fee_min']):.2f}, max {float(cfg['fights']['initiation_fee_max']):.2f})")
    add("togglefight", _toggle_fight(repo),
        lambda cfg: "enable/disable fight requests")
    add("users", _users(repo, cfg), lambda cfg: "show user counts")


def _noop_help():
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is not None:
            await update.message.reply_text(Msg.REACT_USAGE)

    return handler


def _start(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        admin_ids = set(int(x) for x in cfg["bot"].get("admin_ids", []))
        existing = await repo.get_user(user.id)
        first_start = existing is None
        joining_now = existing is None or not existing.has_started
        db_user = await repo.get_or_create_user(
            user.id,
            user.username,
            admin_ids,
            starting_credits=float(cfg["credits"]["starting_balance"]),
        )
        await repo.update_started(user.id, True)
        if not db_user.about_seen:
            about = await repo.get_about()
            sent = await safe_reply_text(update.message, repo, about or Msg.ABOUT_DEFAULT)
            if sent is None:
                return
            await repo.set_about_seen(user.id, True)

        # Invite credit flow
        if first_start and context.args:
            code = context.args[0].strip()
            prefix = str(cfg.get("invites", {}).get("start_prefix", "inv_"))
            if code.startswith(prefix):
                invite = await repo.invite_by_code(code)
                if invite and int(invite["inviter_id"]) != user.id:
                    if await repo.redeem_invite_once(code, user.id):
                        reward = float(cfg["credits"]["invite_reward"])
                        inviter_id = int(invite["inviter_id"])
                        balance, applied = await adjust_credits_with_daily_limit(
                            repo,
                            cfg,
                            inviter_id,
                            reward,
                            "invite_reward",
                        )
                        await repo.clear_cooldown(inviter_id)
                        await safe_send_message(
                            context.bot,
                            repo,
                            inviter_id,
                            text=Msg.invite_used_cooldown_removed(
                                applied, balance),
                        )

        initial_cooldown = int(cfg.get("onboarding", {}).get(
            "initial_cooldown_seconds", 0))
        if joining_now and initial_cooldown > 0:
            until = datetime.now(timezone.utc) + \
                timedelta(seconds=initial_cooldown)
            await repo.set_cooldown(user.id, until.isoformat(), "initial-join", 0)
            minutes = max(1, math.ceil(initial_cooldown / 60))
            await safe_reply_text(update.message, repo, Msg.initial_cooldown(minutes))

    return handler


def _stop(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        await repo.update_started(user.id, False)
        await safe_reply_text(update.message, repo, Msg.STOPPED)

    return handler


def _users(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        days = int(cfg.get("inactivity", {}).get("period_days", 7))
        counts = await repo.user_counts(days)
        text = "\n".join(
            [
                "Users:",
                f"Total: {counts['total']}",
                f"Active (<= {days}d): {counts['active']}",
                f"Inactive (> {days}d): {counts['inactive']}",
                f"Banned: {counts['blacklisted']}",
                f"Left: {counts['left']}",
            ]
        )
        await update.message.reply_text(text)

    return handler


def _help():
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        cfg = context.application.bot_data.get("cfg", {})
        repo = context.application.bot_data.get("repo")
        caller = await repo.get_user(update.effective_user.id) if repo is not None else None
        can_view_mod_commands = bool(caller and (caller.is_moderator or caller.is_admin))
        entries = context.application.bot_data.get("help_entries", [])
        sections: list[str] = []
        for entry in entries:
            if entry.section == "Mod" and not can_view_mod_commands:
                continue
            if entry.section not in sections:
                sections.append(entry.section)
        lines: list[str] = []
        for section in sections:
            lines.append(f"{section} Commands:")
            for entry in entries:
                if entry.section != section:
                    continue
                if entry.section == "Mod" and not can_view_mod_commands:
                    continue
                desc = entry.description(cfg) if callable(
                    entry.description) else str(entry.description)
                lines.append(f"{entry.command} - {desc}")
            lines.append("")
        await update.message.reply_text("\n".join(lines).strip())

    return handler


def _about(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if context.args and user and user.is_admin:
            text = " ".join(context.args).strip()
            if text:
                await repo.set_about(text)
                await update.message.reply_text(Msg.ABOUT_UPDATED)
                return
        text = await repo.get_about()
        await update.message.reply_text(text or Msg.ABOUT_DEFAULT)

    return handler


def _toggle_confirmation(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        new_state = not user.confirmation_enabled
        await repo.set_confirmation_enabled(user.telegram_id, new_state)
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(Msg.enabled("Questionable confirmation", new_state))

    return handler


def _toggle_vote_button(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        new_state = not user.vote_buttons_enabled
        await repo.set_vote_buttons_enabled(user.telegram_id, new_state)
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(Msg.enabled(Msg.VOTE_BUTTON_LABEL, new_state))

    return handler


def _toggle_potentially_unwanted(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        new_state = not user.hide_potentially_unwanted
        await repo.set_hide_potentially_unwanted(user.telegram_id, new_state)
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(Msg.enabled(Msg.POTENTIALLY_UNWANTED_FILTER_LABEL, new_state))

    return handler


def _toggle_duplicates(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        new_state = not user.filter_duplicates
        await repo.set_filter_duplicates(user.telegram_id, new_state)
        await repo.touch_activity(user.telegram_id)
        text = Msg.enabled(Msg.DUPLICATE_FILTER_LABEL, new_state)
        if user.is_moderator or user.is_admin:
            text = f"{text}\n{Msg.MOD_EXEMPT_SETTING}"
        await update.message.reply_text(text)

    return handler


def _toggle_sign(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not bool(cfg.get("identity", {}).get("allow_sign_toggle", False)):
            await update.message.reply_text(Msg.SIGN_TOGGLE_DISABLED)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        await repo.set_sign_enabled(user.telegram_id, not user.sign_enabled)
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(Msg.enabled("Sign", not user.sign_enabled))

    return handler


def _toggle_fight(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        await repo.set_fights_enabled(user.telegram_id, not user.fights_enabled)
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(Msg.enabled("Fight requests", not user.fights_enabled))

    return handler


def _block(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not update.message.reply_to_message:
            await update.message.reply_text(Msg.BLOCK_USAGE)
            return
        lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
        if lookup is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        _, sender_id = lookup
        if sender_id == update.effective_user.id:
            await update.message.reply_text(Msg.CANNOT_BLOCK_SELF)
            return
        user = await repo.get_user(update.effective_user.id)
        await repo.add_block(update.effective_user.id, sender_id)
        await repo.touch_activity(update.effective_user.id)
        text = Msg.BLOCKED_SENDER
        if user is not None and (user.is_moderator or user.is_admin):
            text = f"{text}\n{Msg.MOD_EXEMPT_SETTING}"
        await update.message.reply_text(text)

    return handler


def _unblock(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        removed = await repo.remove_last_block(update.effective_user.id)
        if removed is None:
            await update.message.reply_text(Msg.UNBLOCK_EMPTY)
            return
        await repo.touch_activity(update.effective_user.id)
        await update.message.reply_text(Msg.UNBLOCKED_LAST)

    return handler


def _credit(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        sender = await repo.get_user(update.effective_user.id)
        if sender is None:
            return
        target = None
        amount = None
        source_message_id: int | None = None
        target_reply_to: int | None = None
        if update.message.reply_to_message and context.args:
            try:
                amount = round_credit(float(context.args[0]))
            except ValueError:
                amount = None
            if amount is None or (amount <= 0 and not sender.is_admin):
                await update.message.reply_text(Msg.CREDIT_USAGE_REPLY)
                return
            resolved = await resolve_reply_target(
                repo,
                update.effective_user.id,
                update.message.reply_to_message.message_id,
            )
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.MESSAGE_NOT_IN_CACHE)
                return
            source_message_id = resolved.source_message_id
            target = resolved.user.telegram_id
            if source_message_id is not None and source_message_id > 0:
                target_reply_to = await repo.delivery_message_for_recipient(source_message_id, target)
        elif len(context.args) >= 2:
            resolved = await resolve_user_reference(repo, cfg, sender, context.args)
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.CREDIT_TARGET_NOT_FOUND)
                return
            try:
                amount = round_credit(float(context.args[resolved.consumed]))
            except IndexError:
                await update.message.reply_text(Msg.CREDIT_USAGE_TARGET)
                return
            except ValueError:
                amount = None
            if amount is None:
                await update.message.reply_text(Msg.CREDIT_USAGE_TARGET)
                return
            target = resolved.user.telegram_id
        else:
            await update.message.reply_text(Msg.CREDIT_USAGE)
            return

        if target == sender.telegram_id:
            await update.message.reply_text(Msg.CREDIT_TARGET_SELF)
            return
        if amount is None or amount == 0:
            await update.message.reply_text(Msg.CREDIT_NONZERO)
            return
        if amount < 0 and not sender.is_admin:
            await update.message.reply_text(Msg.CREDIT_NEGATIVE_ADMIN_ONLY)
            return
        if sender.is_admin:
            if await repo.get_user(target) is None:
                await update.message.reply_text(Msg.CREDIT_TRANSFER_INVALID_USER)
                return
            target_balance = await repo.adjust_credits(target, amount, "admin_credit")
            await apply_negative_credit_cooldown(repo, cfg, target, target_balance, sender.telegram_id)
            await repo.touch_activity(sender.telegram_id)
            await _notify_credit_recipient(
                context,
                target,
                amount,
                target_balance,
                target_reply_to,
                admin_adjustment=True,
            )
            await update.message.reply_text(Msg.admin_credit_adjusted(amount, target_balance))
        else:
            success = await repo.credits_transfer(
                sender_id=sender.telegram_id,
                target_id=target,
                amount=amount,
                allow_negative_sender=False,
            )
            if not success:
                await update.message.reply_text(Msg.CREDIT_TRANSFER_FAILED)
                return
            refreshed = await repo.get_user(sender.telegram_id)
            if refreshed is not None:
                await apply_negative_credit_cooldown(repo, cfg, refreshed.telegram_id, refreshed.credits, sender.telegram_id)
            await repo.touch_activity(sender.telegram_id)
            sender_balance = refreshed.credits if refreshed is not None else 0.0
            target_user = await repo.get_user(target)
            if target_user is not None:
                await _notify_credit_recipient(
                    context,
                    target,
                    amount,
                    target_user.credits,
                    target_reply_to,
                    admin_adjustment=False,
                )
            await update.message.reply_text(Msg.credits_transferred(amount, sender_balance))

    return handler


async def _notify_credit_recipient(
    context: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    amount: float,
    balance: float,
    reply_to_message_id: int | None,
    admin_adjustment: bool,
) -> None:
    if admin_adjustment and amount < 0:
        text = f"Your credits were adjusted by {amount:.2f}. Balance: {balance:.2f}"
    elif admin_adjustment:
        text = f"You received {amount:.2f} credits from an admin adjustment. Balance: {balance:.2f}"
    else:
        text = f"You received {amount:.2f} credits. Balance: {balance:.2f}"
    if reply_to_message_id is not None:
        text += "\nThis transfer is attached to the replied message."
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        pass


def _creditstats(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        caller = await repo.get_user(update.effective_user.id)
        if caller is None:
            return
        top_daily = await repo.list_top_credits(since_days=1, limit=10)
        top_all = await repo.list_top_credits(since_days=None, limit=10)
        min_c, med_c, max_c = await repo.get_credit_distribution()
        supply = await repo.current_supply()
        net_daily = await repo.net_issuance_since_days(1)
        net_weekly = await repo.net_issuance_since_days(7)
        start_daily = max(1e-9, supply - net_daily)
        start_weekly = max(1e-9, supply - net_weekly)
        inflation_daily = (net_daily / start_daily) * 100.0
        inflation_weekly = (net_weekly / start_weekly) * 100.0
        tax_rate = interpolate_tax_rate(
            cfg["credits"]["tax_ramp"], caller.credits) * 100.0
        expected = caller.credits * (1.0 - (tax_rate / 100.0))
        loss_rate = interpolate_loss_rate(
            cfg["loss_rate"]["schedule"], caller.credits) * 100.0

        def ident(row: Any) -> str:
            anon = html.escape(temporal_id(
                int(row["telegram_id"]), cfg["bot"]["global_salt"]))
            trip = None
            if row["tripcode_enabled"] and row["tripcode_name"] and row["tripcode_hash"]:
                name = html.escape(str(row["tripcode_name"]))
                trip = f"<b>{name}</b> !{str(row['tripcode_hash'])[:6]}"
            if caller.is_admin:
                parts = [anon]
                if trip:
                    parts.append(trip)
                if row["username"]:
                    parts.append(f"@{html.escape(str(row['username']))}")
                return " ".join(parts)
            return trip or anon

        def limit_label(key: str) -> str:
            return html.escape(key.replace("_", " ").title())

        limits = cfg["credits"].get("daily_earning_limits", {})
        limit_lines = []
        for k, v in limits.items():
            value = float(v)
            rendered = "unlimited" if value < 0 else f"{value:.2f}"
            limit_lines.append(f"{limit_label(k)}: {rendered}")

        cost_lines = [
            f"Unsend: {float(cfg['credits']['unsend_cost']):.2f}",
            f"Edit: {float(cfg['credits'].get('edit_cost', 2.0)):.2f}",
            f"Whisper: {float(cfg['credits']['whisper_cost']):.2f}",
            f"Upvote: {float(cfg['credits']['upvote_cost']):.2f}",
            f"Downvote Start: {float(cfg['credits']['downvote_start_cost']):.2f}",
            f"Downvote Penalty To Sender: {float(cfg['credits']['downvote_penalty']):.2f}",
            f"Fight Fee: {float(cfg['fights']['initiation_fee_percent']) * 100.0:.2f}% "
            f"(min {float(cfg['fights']['initiation_fee_min']):.2f}, max {float(cfg['fights']['initiation_fee_max']):.2f})",
            f"Fight Win Tax: {float(cfg['fights']['win_tax_percent']) * 100.0:.2f}%",
            f"Remove Punishment: {float(cfg['vote_to_remove']['punishment_credit_tax_percent']) * 100.0:.2f}% "
            f"(min {float(cfg['vote_to_remove']['punishment_credit_minimum']):.2f})",
            f"Revert Punishment: {float(cfg['vote_to_remove']['reversal_punishment_credit_tax_percent']) * 100.0:.2f}% "
            f"(min {float(cfg['vote_to_remove']['reversal_punishment_credit_minimum']):.2f})",
        ]
        downvote_schedule = [
            f"minute {x['minute']}: {float(x['cost']):.2f}"
            for x in cfg["credits"].get("downvote_cost_schedule", [])
        ]
        loss_schedule = [
            f"{float(x['credits']):.2f} credits: {float(x['loss_rate']) * 100.0:.2f}%"
            for x in cfg["loss_rate"].get("schedule", [])
        ]

        daily_lines = [
            f"{i+1}. {ident(r)} - {float(r['earned'] if 'earned' in r.keys() else r['credits']):.2f}"
            for i, r in enumerate(top_daily)
        ]
        all_lines = [
            f"{i+1}. {ident(r)} - {float(r['credits']):.2f}" for i, r in enumerate(top_all)]
        text = "\n".join(
            [
                "<b>Top 10 All Time</b>",
                *(all_lines or ["- none"]),
                "",
                "<b>Top 10 Daily</b>",
                *(daily_lines or ["- none"]),
                "",
                "<b>Inflation</b>",
                f"Daily: {inflation_daily:.2f}%",
                f"Weekly: {inflation_weekly:.2f}%",
                "",
                "<b>Credit Summary</b>",
                f"Minimum / Median / Maximum: {min_c:.2f} / {med_c:.2f} / {max_c:.2f}",
                f"Your Current Balance: {caller.credits:.2f}",
                f"Your Daily Tax Rate: {tax_rate:.2f}%",
                f"Your Loss Rate: {loss_rate:.2f}%",
                f"Your Expected Balance (1d): {expected:.2f}",
                "",
                "<b>Loss Rate Settings</b>",
                *(loss_schedule or ["- none"]),
                "",
                "<b>Credit Config</b>",
                f"Starting Balance: {float(cfg['credits']['starting_balance']):.2f}",
                f"Text Reward: {float(cfg['credits']['text_message_reward']):.2f}",
                f"Media Reward: {float(cfg['credits']['media_message_reward']):.2f}",
                f"Upvote Reward: {float(cfg['credits']['upvote_reward']):.2f}",
                f"Invite Reward: {float(cfg['credits']['invite_reward']):.2f}",
                *(cost_lines or ["- none"]),
                "",
                "<b>Downvote Cost Schedule:</b>",
                *(downvote_schedule or ["- none"]),
                "",
                "<b>Daily Limits:</b>",
                *(limit_lines or ["- none"]),
            ]
        )
        await update.message.reply_text(text, parse_mode="HTML")

    return handler


def _gamble(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not context.args:
            await update.message.reply_text(Msg.GAMBLE_USAGE)
            return
        try:
            amount = round_credit(float(context.args[0]))
        except ValueError:
            await update.message.reply_text(Msg.AMOUNT_NUMERIC)
            return
        if amount < 0.01:
            await update.message.reply_text(Msg.AMOUNT_MIN)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        if amount > user.credits:
            await update.message.reply_text(Msg.GAMBLE_TOO_MUCH)
            return
        if amount > float(cfg.get("gamble", {}).get("max_amount", 1000.0)):
            await update.message.reply_text(Msg.GAMBLE_MAX)
            return
        if random.random() < 0.5:
            bal, applied = await adjust_credits_with_daily_limit(repo, cfg, user.telegram_id, amount, "gamble_win")
            await repo.touch_activity(user.telegram_id)
            await update.message.reply_text(Msg.gamble_won(applied, bal))
        else:
            bal = await repo.adjust_credits(user.telegram_id, -amount, "gamble_loss")
            await apply_negative_credit_cooldown(repo, cfg, user.telegram_id, bal, user.telegram_id)
            await repo.touch_activity(user.telegram_id)
            await update.message.reply_text(Msg.gamble_lost(amount, bal))

    return handler


def _set_tripcode(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not context.args:
            await update.message.reply_text(Msg.TRIPCODESET_USAGE)
            return
        value = " ".join(context.args).strip()
        if "#" not in value:
            await update.message.reply_text(Msg.TRIPCODESET_USAGE)
            return
        name, secret = value.split("#", 1)
        name = name.strip()
        secret = secret.strip()
        if not name or not secret:
            await update.message.reply_text(Msg.TRIPCODESET_USAGE)
            return
        hashed = hash_tripcode(secret, cfg["bot"]["global_salt"])
        await repo.set_tripcode(update.effective_user.id, True, name, hashed)
        await repo.touch_activity(update.effective_user.id)
        await update.message.reply_text(
            Msg.tripcode_set(html.escape(name), hashed[:6]),
            parse_mode="HTML",
        )

    return handler


def _unset_tripcode(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        await repo.set_tripcode(update.effective_user.id, False, None, None)
        await repo.touch_activity(update.effective_user.id)
        await update.message.reply_text(Msg.TRIPCODE_UNSET)

    return handler


def _tripcode_send(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not context.args:
            await update.message.reply_text(Msg.TRIPCODE_SEND_USAGE)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        if not user.tripcode_name or not user.tripcode_hash:
            await update.message.reply_text(Msg.TRIPCODE_SET_FIRST)
            return
        msg = _apply_identity_text(user, " ".join(
            context.args).strip(), mode="tripcode")
        if await _dispatch_user_message(context, repo, cfg, user.telegram_id, msg, update.message, parse_mode="HTML"):
            await update.message.reply_text(Msg.TRIPCODE_SENT)

    return handler


def _toggle_tripcode(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not bool(cfg.get("identity", {}).get("allow_tripcode_toggle", False)):
            await update.message.reply_text(Msg.TRIPCODE_TOGGLE_DISABLED)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        enabled = not user.tripcode_enabled
        await repo.set_tripcode(update.effective_user.id, enabled, user.tripcode_name, user.tripcode_hash)
        await repo.touch_activity(update.effective_user.id)
        await update.message.reply_text(Msg.enabled("Tripcode", enabled))

    return handler


async def _dispatch_user_message(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    cfg: dict[str, Any],
    sender_id: int,
    text: str,
    source_message: Any | None = None,
    parse_mode: str | None = None,
    include_remove_button: bool = False,
) -> bool:
    queue = context.application.bot_data["queue"]
    source_chat_id = getattr(source_message, "chat_id", None)
    source_message_id = getattr(source_message, "message_id", None)
    reply_to_message_id = None
    if source_message is not None and getattr(source_message, "reply_to_message", None):
        reply_msg = source_message.reply_to_message
        lookup = await repo.sender_by_delivery(sender_id, reply_msg.message_id)
        if lookup is None:
            source = await repo.message_by_source(reply_msg.chat_id, reply_msg.message_id)
            if source is None:
                await source_message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
                return False
            reply_to_message_id = int(source["id"])
        else:
            reply_to_message_id, _ = lookup
    message_id = await repo.create_message(
        sender_id,
        "text",
        text,
        None,
        None,
        source_chat_id,
        source_message_id,
        reply_to_message_id,
        parse_mode,
    )
    await repo.set_message_tag(message_id, "OK", None)
    recipients = await repo.list_eligible_recipients(sender_id)
    await queue.enqueue_batch(
        message_id,
        sender_id,
        recipients,
        "text",
        text,
        None,
        None,
        is_system=False,
        reply_to_message_id=reply_to_message_id,
        include_remove_button=include_remove_button,
        parse_mode=parse_mode,
    )
    await adjust_credits_with_daily_limit(
        repo,
        cfg,
        sender_id,
        float(cfg["credits"]["text_message_reward"]),
        "text_message_reward",
    )
    await repo.touch_activity(sender_id)
    return True


def _apply_identity_text(user: Any, text: str, mode: str = "default") -> str:
    body = html.escape(text)
    if mode == "sign" or (mode == "default" and user.sign_enabled):
        label = f"@{html.escape(user.username)}" if user.username else "signed"
        return f"{body} <i><b>~ {label}</b></i>"
    if (mode == "tripcode" or (mode == "default" and user.tripcode_enabled)) and user.tripcode_name and user.tripcode_hash:
        name = html.escape(str(user.tripcode_name))
        return f"<b>{name}</b> !{str(user.tripcode_hash)[:6]}\n{body}"
    return body


def _whisper_html(text: str, label: str = "Whisper", text_is_html: bool = False) -> str:
    body = text if text_is_html else html.escape(text)
    return f"<i><b>{label}:</b></i> {body}"


def _signed_send(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not context.args:
            await update.message.reply_text(Msg.SIGN_SEND_USAGE)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        msg = _apply_identity_text(user, " ".join(
            context.args).strip(), mode="sign")
        if await _dispatch_user_message(context, repo, cfg, user.telegram_id, msg, update.message, parse_mode="HTML"):
            await update.message.reply_text(Msg.SIGNED_SENT)

    return handler


def _send_invite_message(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        text = " ".join(context.args).strip()
        match = TELEGRAM_INVITE_RE.search(text)
        if match is None:
            await update.message.reply_text(Msg.SENDINVITE_USAGE)
            return
        description = (text[:match.start()] + " " + text[match.end():]).strip()
        if len(description) < 3:
            await update.message.reply_text(Msg.SENDINVITE_USAGE)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        if await _dispatch_user_message(
            context,
            repo,
            cfg,
            user.telegram_id,
            text,
            update.message,
            include_remove_button=True,
        ):
            await update.message.reply_text(Msg.SENDINVITE_SENT)

    return handler


def _invite(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        prefix = str(cfg.get("invites", {}).get("start_prefix", "inv_"))
        existing = await repo.invite_by_inviter(update.effective_user.id)
        if existing is not None:
            code = str(existing["invite_code"])
        else:
            while True:
                code = f"{prefix}{secrets.token_urlsafe(8)}"
                if await repo.invite_by_code(code) is None:
                    break
            await repo.upsert_invite(update.effective_user.id, code)
        await repo.touch_activity(update.effective_user.id)
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={code}" if me.username else f"start code: {code}"
        await update.message.reply_text(Msg.invite_link(link))

    return handler


def _unsend(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        cost = float(cfg["credits"]["unsend_cost"])
        if user.credits < cost:
            await update.message.reply_text(Msg.unsend_insufficient(cost, user.credits))
            return
        if update.message.reply_to_message is None:
            await update.message.reply_text(Msg.UNSEND_USAGE)
            return
        target = await repo.message_by_source(
            update.effective_user.id,
            update.message.reply_to_message.message_id,
        )
        if target is None:
            lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
            if lookup is not None:
                message_id, sender_id = lookup
                if sender_id == user.telegram_id:
                    target = await repo.get_message(message_id)
        if target is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        if int(target["sender_id"]) != user.telegram_id:
            await update.message.reply_text(Msg.UNSEND_OWN_ONLY)
            return
        message_id = int(target["id"])
        if str(target["tag"]) == "BLOCKED":
            await update.message.reply_text(Msg.UNSEND_NOT_SENT)
            return
        if not await repo.list_deliveries_for_message(message_id):
            await update.message.reply_text(Msg.UNSEND_NOT_DELIVERED)
            return
        await remove_message_with_tombstones(
            context,
            repo,
            cfg,
            message_id,
            user.telegram_id,
            "unsent by sender",
            notify_mods=True,
            notify_sender=False,
        )
        balance = await repo.adjust_credits(user.telegram_id, -cost, "unsend_cost")
        await repo.touch_activity(user.telegram_id)
        await update.message.reply_text(
            Msg.unsent(cost, balance),
            reply_to_message_id=update.message.reply_to_message.message_id,
        )

    return handler


def _delete_vote(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None or not update.message.reply_to_message:
            if update.message:
                await update.message.reply_text(Msg.DELETEVOTE_USAGE)
            return
        lookup = await repo.sender_by_delivery(update.effective_user.id, update.message.reply_to_message.message_id)
        target_message = None
        if lookup is None:
            target_message = await repo.message_by_source(
                update.effective_user.id,
                update.message.reply_to_message.message_id,
            )
            if target_message is not None:
                lookup = (int(target_message["id"]), int(
                    target_message["sender_id"]))
        if lookup is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        message_id, sender_id = lookup
        if target_message is None:
            target_message = await repo.get_message(message_id)
        if target_message is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        caller = await repo.get_user(update.effective_user.id)
        if caller is None:
            return
        if sender_id == update.effective_user.id:
            await update.message.reply_text(Msg.DELETEVOTE_OWN)
            return
        allowed, reason = await check_remove_vote_allowed(repo, cfg, update.effective_user.id)
        if not allowed:
            await update.message.reply_text(reason or "Remove vote unavailable.")
            return
        if not await repo.add_remove_vote(message_id, update.effective_user.id):
            await update.message.reply_text(Msg.DELETEVOTE_ALREADY)
            return
        await repo.touch_activity(update.effective_user.id)
        count = await repo.count_remove_votes(message_id)
        threshold = int(cfg["vote_to_remove"]["threshold"])
        if count < threshold:
            await update.message.reply_text(Msg.remove_vote_counted(count, threshold))
            return
        await remove_message_with_tombstones(context, repo, cfg, message_id, sender_id, "community vote threshold reached")
        await _notify_delete_vote_participants(context, repo, message_id)
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

    return handler


async def _notify_delete_vote_participants(
    context: ContextTypes.DEFAULT_TYPE,
    repo: Any,
    message_id: int,
) -> None:
    for voter_id in await repo.list_remove_voters(message_id):
        reply_to = await repo.delivery_or_tombstone_message_for_recipient(message_id, voter_id)
        try:
            await context.bot.send_message(
                chat_id=voter_id,
                text=Msg.DELETEVOTE_DELETED_NOTIFY,
                reply_to_message_id=reply_to,
            )
        except Exception:
            pass


def _sauce(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None or not update.message.reply_to_message:
            if update.message:
                await update.message.reply_text(Msg.SAUCE_USAGE)
            return
        sauce_cfg = cfg.get("saucenao", {})
        api_key = str(sauce_cfg.get("api_key") or "").strip()
        if api_key.startswith("${"):
            api_key = ""
        if not bool(sauce_cfg.get("enabled", True)) or not api_key:
            await update.message.reply_text(Msg.SAUCE_DISABLED)
            return
        user = await repo.get_user(update.effective_user.id)
        if user is None:
            return
        fraction = float(sauce_cfg.get("top_credit_percentile", -1))
        if fraction > 1:
            fraction = fraction / 100.0
        if fraction >= 0:
            required = await repo.credit_cutoff_for_top_fraction(fraction)
            if user.credits < required:
                await update.message.reply_text(Msg.sauce_credit_required(required, user.credits))
                return
        per_user_limit = int(sauce_cfg.get("per_user_daily_limit", 3))
        if per_user_limit >= 0 and await repo.sauce_usage_count(user.telegram_id) >= per_user_limit:
            await update.message.reply_text(Msg.SAUCE_LIMITED)
            return
        global_limit = int(sauce_cfg.get("global_daily_limit", 95))
        if global_limit >= 0 and await repo.sauce_total_usage_count() >= global_limit:
            await update.message.reply_text(Msg.SAUCE_LIMITED)
            return

        lookup = await repo.sender_by_delivery(user.telegram_id, update.message.reply_to_message.message_id)
        target = None
        if lookup is not None:
            target = await repo.get_message(lookup[0])
        else:
            target = await repo.message_by_source(user.telegram_id, update.message.reply_to_message.message_id)
        if target is None:
            await update.message.reply_text(Msg.MESSAGE_NOT_IN_CACHE)
            return
        message_id = int(target["id"])
        delivery = await repo.delivery_for_recipient(message_id, user.telegram_id)
        if delivery is not None and bool(delivery.get("is_blurred")):
            await update.message.reply_text(Msg.SAUCE_BLURRED, reply_to_message_id=update.message.reply_to_message.message_id)
            return
        cached = await repo.get_sauce_cache(message_id)
        if cached is not None:
            await update.message.reply_text(_format_sauce_cache(cached, cached=True), reply_to_message_id=update.message.reply_to_message.message_id)
            return

        media_kind = str(target.get("media_kind") or "")
        if media_kind in {"video", "animation", "video_note", "sticker"}:
            file_id = target.get("thumbnail_file_id") or target.get("media_file_id")
        else:
            file_id = target.get("media_file_id") or target.get("thumbnail_file_id")
        if not file_id:
            await update.message.reply_text(Msg.SAUCE_NO_MEDIA)
            return
        try:
            tg_file = await context.bot.get_file(str(file_id))
            file_url = str(tg_file.file_path)
            result = await _fetch_sauce(api_key, file_url, int(sauce_cfg.get("num_results", 6)))
        except Exception:
            logger.exception("SauceNAO lookup failed message_id=%s user_id=%s", message_id, user.telegram_id)
            await update.message.reply_text(Msg.SAUCE_LOOKUP_FAILED)
            return
        await repo.add_sauce_usage(user.telegram_id)
        if result is None:
            await update.message.reply_text(Msg.SAUCE_NO_RESULTS, reply_to_message_id=update.message.reply_to_message.message_id)
            return
        await repo.set_sauce_cache(message_id, result)
        await update.message.reply_text(_format_sauce_cache(result, cached=False), reply_to_message_id=update.message.reply_to_message.message_id)

    return handler


async def _fetch_sauce(api_key: str, file_url: str, num_results: int) -> dict[str, Any] | None:
    from saucenao_api import AIOSauceNao  # type: ignore[import-not-found]

    async with AIOSauceNao(api_key, numres=num_results) as sauce:
        results = await sauce.from_url(file_url)
    if not results:
        return None
    best = results[0]
    return {
        "title": str(getattr(best, "title", "") or ""),
        "similarity": float(getattr(best, "similarity", 0.0) or 0.0),
        "author": str(getattr(best, "author", "") or ""),
        "urls": [str(x) for x in (getattr(best, "urls", None) or [])],
        "thumbnail": str(getattr(best, "thumbnail", "") or ""),
        "short_remaining": getattr(results, "short_remaining", None),
        "long_remaining": getattr(results, "long_remaining", None),
    }


def _format_sauce_cache(result: dict[str, Any], cached: bool) -> str:
    return Msg.sauce_result(
        title=str(result.get("title") or ""),
        similarity=float(result.get("similarity") or 0.0),
        author=str(result.get("author") or "") or None,
        urls=[str(x) for x in (result.get("urls") or [])],
        cached=cached,
        short_remaining=result.get("short_remaining"),
        long_remaining=result.get("long_remaining"),
    )


def _w(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        sender = await repo.get_user(update.effective_user.id)
        if sender is None:
            return
        sender_is_privileged = bool(sender.is_moderator or sender.is_admin)
        unlock = float(cfg["credits"]["whisper_unlock_credits"])
        if not sender_is_privileged and sender.credits < unlock:
            await update.message.reply_text(Msg.whisper_unlock_required(unlock))
            return
        target_id = None
        text = None
        reply_is_whisper = False
        replied_message_id = None
        replied_whisper_id = None
        if update.message.reply_to_message and context.args:
            resolved = await resolve_reply_target(
                repo,
                update.effective_user.id,
                update.message.reply_to_message.message_id,
            )
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.MESSAGE_NOT_IN_CACHE)
                return
            target_id = resolved.user.telegram_id
            if resolved.source_message_id is not None and resolved.source_message_id < 0:
                replied_whisper_id = -resolved.source_message_id
                reply_is_whisper = True
            else:
                replied_message_id = resolved.source_message_id
            text = " ".join(context.args).strip()
        elif len(context.args) >= 2:
            resolved = await resolve_user_reference(repo, cfg, sender, context.args)
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.WHISPER_USAGE)
                return
            target_id = resolved.user.telegram_id
            text = " ".join(context.args[resolved.consumed:]).strip()
        if target_id is None or not text:
            await update.message.reply_text(Msg.WHISPER_USAGE)
            return
        if target_id == sender.telegram_id:
            await update.message.reply_text(Msg.WHISPER_CANNOT_SELF)
            return
        identity_mode = "default"
        if text.startswith("/s "):
            identity_mode = "sign"
            text = text[3:].strip()
        elif text.startswith("/t "):
            identity_mode = "tripcode"
            text = text[3:].strip()
            if not sender.tripcode_name or not sender.tripcode_hash:
                await update.message.reply_text(Msg.TRIPCODE_SET_FIRST)
                return
        text = _apply_identity_text(sender, text, mode=identity_mode)
        text_is_html = True
        cost = 0.0 if sender_is_privileged else float(cfg["credits"]["whisper_cost"])
        if cost > 0 and sender.credits < cost:
            await update.message.reply_text(Msg.WHISPER_INSUFFICIENT_CREDITS)
            return
        balance = float(sender.credits)
        if cost > 0:
            balance = await repo.adjust_credits(sender.telegram_id, -cost, "whisper_cost")
        await repo.touch_activity(sender.telegram_id)
        whisper_id = await repo.create_whisper(sender.telegram_id, target_id, text, False)
        if update.message is not None:
            await repo.add_whisper_delivery(whisper_id, sender.telegram_id, update.message.message_id)
        try:
            reply_to = None
            if replied_whisper_id is not None:
                reply_to = await repo.whisper_delivery_message_id(replied_whisper_id, target_id)
            elif replied_message_id is not None:
                reply_to = await repo.delivery_or_tombstone_message_for_recipient(replied_message_id, target_id)
            sent = await context.bot.send_message(
                chat_id=target_id,
                text=_whisper_html(text, text_is_html=text_is_html),
                parse_mode="HTML",
                reply_to_message_id=reply_to,
            )
            await repo.add_whisper_delivery(whisper_id, target_id, sent.message_id)
        except Exception:
            await update.message.reply_text(Msg.WHISPER_UNAVAILABLE)
            return
        mods = await repo.list_mod_and_admin_users()
        for m in mods:
            if m.telegram_id in {sender.telegram_id, target_id}:
                continue
            try:
                mod_reply_to = None
                if replied_whisper_id is not None:
                    mod_reply_to = await repo.whisper_delivery_message_id(replied_whisper_id, m.telegram_id)
                elif replied_message_id is not None:
                    mod_reply_to = await repo.delivery_or_tombstone_message_for_recipient(replied_message_id, m.telegram_id)
                sent = await context.bot.send_message(
                    chat_id=m.telegram_id,
                    text=_whisper_html(text, text_is_html=text_is_html),
                    parse_mode="HTML",
                    reply_to_message_id=mod_reply_to,
                )
                await repo.add_whisper_delivery(whisper_id, m.telegram_id, sent.message_id)
            except Exception:
                pass
        await update.message.reply_text(Msg.whisper_sent(cost, balance))

    return handler


def _whisper_mod(repo: Any):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None or not context.args:
            if update.message:
                await update.message.reply_text(Msg.WHISPERMOD_USAGE)
            return
        sender = await repo.get_user(update.effective_user.id)
        if sender is None:
            return
        text = " ".join(context.args).strip()
        await repo.touch_activity(sender.telegram_id)
        mods = await repo.list_mod_and_admin_users()
        for m in mods:
            if m.telegram_id == sender.telegram_id:
                continue
            try:
                sent = await context.bot.send_message(chat_id=m.telegram_id, text=_whisper_html(text, "Modwhisper"), parse_mode="HTML")
                wid = await repo.create_whisper(sender.telegram_id, m.telegram_id, text, True)
                if update.message is not None:
                    await repo.add_whisper_delivery(wid, sender.telegram_id, update.message.message_id)
                await repo.add_whisper_delivery(wid, m.telegram_id, sent.message_id)
            except Exception:
                pass
        await update.message.reply_text(Msg.WHISPERMOD_SENT)

    return handler


def _fight(repo: Any, cfg: dict[str, Any]):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            if update.message:
                await update.message.reply_text(Msg.FIGHT_USAGE)
            return
        if not bool(cfg.get("fights", {}).get("enabled", True)):
            await update.message.reply_text(Msg.FIGHTS_DISABLED)
            return
        sender = await repo.get_user(update.effective_user.id)
        if sender is None:
            return
        target = None
        amount_arg_idx = 0
        if update.message.reply_to_message:
            resolved = await resolve_reply_target(
                repo,
                sender.telegram_id,
                update.message.reply_to_message.message_id,
            )
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.MESSAGE_NOT_IN_CACHE)
                return
            target = resolved.user
        elif context.args:
            resolved = await resolve_user_reference(repo, cfg, sender, context.args)
            if resolved.user is None:
                await update.message.reply_text(resolved.error or Msg.FIGHT_UNAVAILABLE)
                return
            target = resolved.user
            amount_arg_idx = resolved.consumed
        else:
            await update.message.reply_text(Msg.FIGHT_USAGE)
            return
        if target is None:
            await update.message.reply_text(Msg.FIGHT_UNAVAILABLE)
            return
        if not target.fights_enabled:
            await update.message.reply_text(Msg.FIGHT_UNAVAILABLE)
            return
        if target.telegram_id == sender.telegram_id:
            await update.message.reply_text(Msg.FIGHT_SELF)
            return

        max_stake = float(cfg["fights"]["max_stake_cap"])
        if len(context.args) > amount_arg_idx:
            try:
                stake = round_credit(float(context.args[amount_arg_idx]))
            except ValueError:
                await update.message.reply_text(Msg.FIGHT_INVALID_STAKE)
                return
            if stake < 0.01 or stake > max_stake:
                await update.message.reply_text(Msg.FIGHT_INITIATE_FAILED)
                return
        else:
            stake = round_credit(
                min(max_stake, max(1.0, math.sqrt(max(0.0, sender.credits)))))
        if sender.credits < stake:
            await update.message.reply_text(Msg.FIGHT_INITIATE_FAILED)
            return

        # Initiation cooldown (sender-side only).
        latest = await repo.latest_fight_by_initiator(sender.telegram_id)
        if latest is not None:
            try:
                last_dt = as_utc(str(latest["created_at"]))
            except ValueError:
                last_dt = datetime.now(timezone.utc) - timedelta(days=1)
            cooldown_seconds = int(cfg["fights"]["cooldown_seconds"])
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < cooldown_seconds:
                await update.message.reply_text(Msg.FIGHT_INITIATE_FAILED)
                return
        fee_pct = float(cfg["fights"]["initiation_fee_percent"])
        fee_min = float(cfg["fights"]["initiation_fee_min"])
        fee_max = float(cfg["fights"]["initiation_fee_max"])
        fee = round_credit(max(fee_min, min(fee_max, stake * fee_pct)))
        if sender.credits < stake + fee:
            await update.message.reply_text(Msg.FIGHT_INITIATE_FAILED)
            return
        balance = await repo.adjust_credits(sender.telegram_id, -fee, "fight_initiation_fee")
        await repo.touch_activity(sender.telegram_id)
        expires = datetime.now(
            timezone.utc) + timedelta(seconds=int(cfg["fights"]["request_timeout_seconds"]))
        fight_id = await repo.create_fight_request(
            sender.telegram_id,
            target.telegram_id,
            stake,
            fee,
            expires.isoformat(),
            update.message.message_id,
        )

        # Relative descriptor only.
        def tier(c: float) -> int:
            return int(math.log2(max(1.0, c)))

        diff = tier(sender.credits) - tier(target.credits)
        rel = "even"
        if diff >= 2:
            rel = "advantage"
        elif diff == 1:
            rel = "slight advantage"
        elif diff == -1:
            rel = "slight disadvantage"
        elif diff <= -2:
            rel = "disadvantage"
        try:
            await context.bot.send_message(
                chat_id=target.telegram_id,
                text=Msg.fight_request(stake, rel),
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "Accept", callback_data=f"facc:{fight_id}"),
                        InlineKeyboardButton(
                            "Decline", callback_data=f"fdec:{fight_id}"),
                    ]]
                ),
            )
        except Exception:
            await update.message.reply_text(Msg.FIGHT_UNAVAILABLE)
            return
        await update.message.reply_text(Msg.fight_request_sent(fee, balance))

    return handler
