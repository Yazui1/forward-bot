from __future__ import annotations

import math
import logging
import random
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from forward_bot.commands.common import (
    args_text,
    command_reply,
    display_identity,
    display_identity_html,
    ensure_user,
    get_config,
    get_repo,
    get_store,
    is_admin,
    reply_to_for_target,
    resolve_message_from_reply,
    resolve_replied_sender,
    resolve_target_user,
    resolve_user_reference,
    touch_activity,
)
from forward_bot.commands.help_registry import HelpRegistry
from forward_bot.crypto.tripcode import make_tripcode
from forward_bot.features.credits import apply_credit, daily_caps, loss_rate, maybe_apply_negative_cooldown, tax_rate
from forward_bot.features.onboarding import onboarding_prompt, requires_onboarding_answers
from forward_bot.features.remove_votes import vote_to_remove
from forward_bot.features.tombstones import remove_message
from forward_bot.logging_utils import log_telegram_error
from forward_bot.utils import html_escape, human_seconds, mean_median, random_token, round_credits


LOGGER = logging.getLogger(__name__)


def register_user_commands(registry: HelpRegistry) -> None:
    add = registry.add
    add("start", "Lifecycle", "Start receiving messages.", start)
    add("stop", "Lifecycle", "Stop receiving messages.", stop)
    add("help", "Info", "Show available commands.", help_cmd)
    add("about", "Info", "Show bot rules/about text.", about)
    add("users", "Info", "Show user counts.", users_cmd)
    add("info", "Info", "Show your info, or target info for mods/admins.", info_cmd)
    add("toggleconfirmation", "Preferences", "Toggle questionable-message confirmations.", toggle_confirmation)
    add("togglevotebutton", "Preferences", "Toggle inline remove-vote buttons.", toggle_vote_button)
    add("togglepotentiallyunwanted", "Preferences", "Toggle hiding potentially unwanted messages.", toggle_puw)
    add("toggledups", "Preferences", "Toggle duplicate-media recipient filtering.", toggle_dups)
    add("togglesign", "Identity", "Toggle persistent signing when enabled in config.", toggle_sign)
    add("toggletripcode", "Identity", "Toggle persistent tripcode when enabled in config.", toggle_tripcode)
    add("settripcode", "Identity", "Set tripcode with name#secret.", set_tripcode)
    add("unsettripcode", "Identity", "Clear tripcode.", unset_tripcode)
    add("s", "Identity", "Send one signed message.", signed_send)
    add("t", "Identity", "Send one tripcoded message.", tripcode_send)
    add("block", "Safety", "Block a sender by reply or visible user reference.", block)
    add("unblock", "Safety", "Remove your most recent block.", unblock)
    add("credit", "Credits", "Transfer credits by reply or reference.", credit)
    add("creditstats", "Credits", "Show credit leaderboard and economy details.", creditstats)
    add("gamble", "Credits", "Gamble credits with 50% odds.", gamble)
    add("invite", "Invites", "Create or show your invite link.", invite)
    add("sendinvite", "Invites", "Forward a described Telegram invite link.", sendinvite)
    add("unsend", "Moderation", "Remove your own replied message for a cost.", unsend)
    add("deletevote", "Moderation", "Vote to remove a replied message. You can also react with ✍️ on a message.", deletevote)
    add("reactions", "Preferences", "Toggle vote reaction notifications.", reactions)
    add("w", "Whispers", "Whisper to a user by reply or reference.", whisper)
    add("wmods", "Whispers", "Whisper to moderators/admins.", wmods)
    add("sauce", "Media", "Search source for replied media.", sauce)
    add("fight", "Fights", "Challenge a user for credits.", fight)
    add("togglefight", "Fights", "Toggle receiving fight requests.", togglefight)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    repo = get_repo(context)
    config = get_config(context)
    user, created = await ensure_user(update, context)
    if not user:
        return
    if user.is_banned:
        await msg.reply_text("You are banned.")
        return
    first_seen = created
    joining_now = not user.has_started
    repo.set_started(user.telegram_id, True)
    touch_activity(context, user.telegram_id)
    if not user.about_seen:
        await msg.reply_text(repo.get_about())
        repo.set_about_seen(user.telegram_id)
    if first_seen and context.args:
        invite_info = repo.redeem_invite(context.args[0], user.telegram_id)
        if invite_info:
            inviter_id = int(invite_info["inviter_id"])
            reward, inviter = apply_credit(repo, config, inviter_id, float(config.get("credits.invite_reward", 5) or 5), "invite_reward")
            repo.clear_cooldown(inviter_id)
            if inviter and inviter.has_started:
                try:
                    await context.bot.send_message(inviter_id, f"Invite used. Reward: {reward:.2f} credits. Balance: {inviter.credits:.2f}. Cooldown cleared.")
                except TelegramError as exc:
                    log_telegram_error(LOGGER, "invite.notify_inviter", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=inviter_id)
                    pass
    if joining_now:
        initial = int(config.get("onboarding.initial_cooldown_seconds", 0) or 0)
        prompt = onboarding_prompt(user, repo) if requires_onboarding_answers(user, repo) else ""
        if initial > 0 and not user.is_mod_or_admin:
            repo.set_cooldown(user.telegram_id, initial, "onboarding", None, stack=False)
            text = f"Started. Initial cooldown: {human_seconds(initial)}. Invites can clear inviter cooldowns."
        else:
            text = "Started."
        if prompt:
            text = f"{text}\n\n{prompt}"
        await msg.reply_text(text)
    else:
        prompt = onboarding_prompt(user, repo) if requires_onboarding_answers(user, repo) else ""
        if prompt:
            await msg.reply_text(f"Receiving enabled.\n\n{prompt}")
        else:
            await msg.reply_text("Receiving enabled.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if user:
        get_repo(context).set_started(user.telegram_id, False)
    if update.effective_message:
        await update.effective_message.reply_text("Receiving stopped. Use /start to re-enable.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    registry: HelpRegistry = context.application.bot_data["help_registry"]
    await update.effective_message.reply_text(
        registry.help_text(
            include_mod=bool(user and user.is_mod_or_admin),
            include_admin=bool(user and user.is_admin),
            config=get_config(context),
        )
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    repo = get_repo(context)
    text = _command_payload(update, "about")
    if text and is_admin(user):
        repo.set_about(text)
        await command_reply(update, context, "About text updated until reload/restart.")
    elif text:
        await command_reply(update, context, "Admin only.")
    else:
        await command_reply(update, context, repo.get_about(), prefer_target=False)


def _command_payload(update: Update, command: str) -> str:
    msg = update.effective_message
    text = msg.text or msg.caption or "" if msg else ""
    match = re.match(rf"^/{re.escape(command)}(?:@\w+)?(?:\s+([\s\S]*))?$", text)
    return (match.group(1) or "").strip() if match else ""


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo = get_repo(context)
    config = get_config(context)
    period_days = float(config.get("inactivity.period_days", 4) or 4)
    from forward_bot.utils import now_utc, parse_dt

    users = repo.list_users()
    active = inactive = banned = left = 0
    for user in users:
        if user.is_banned:
            banned += 1
        if not user.has_started:
            left += 1
        elif user.is_banned:
            pass
        else:
            last = parse_dt(user.last_activity)
            if last and (now_utc() - last).total_seconds() < period_days * 86400:
                active += 1
            else:
                inactive += 1
    await update.effective_message.reply_text(f"Total: {len(users)}\nActive: {active}\nInactive: {inactive}\nBanned: {banned}\nLeft: {left}")


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from forward_bot.commands.mod_commands import info as mod_info
    await mod_info(update, context)


async def _toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, column: str, label: str) -> None:
    user, _ = await ensure_user(update, context)
    if not user:
        return
    repo = get_repo(context)
    new = not bool(getattr(user, column))
    repo.set_preference(user.telegram_id, column, new)
    touch_activity(context, user.telegram_id)
    await update.effective_message.reply_text(f"{label}: {'on' if new else 'off'}")


async def toggle_confirmation(update, context): await _toggle(update, context, "confirmation_enabled", "Confirmations")
async def toggle_vote_button(update, context): await _toggle(update, context, "vote_buttons_enabled", "Vote buttons")
async def toggle_puw(update, context): await _toggle(update, context, "hide_potentially_unwanted", "Hide potentially unwanted")
async def toggle_dups(update, context): await _toggle(update, context, "filter_duplicates", "Duplicate filtering")
async def reactions(update, context): await _toggle(update, context, "votes_enabled", "Vote notifications")
async def togglefight(update, context): await _toggle(update, context, "fights_enabled", "Fight requests")


async def toggle_sign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_config(context).get("identity.allow_sign_toggle", True):
        await update.effective_message.reply_text("Persistent signing is disabled by config.")
        return
    await _toggle(update, context, "sign_enabled", "Persistent signing")


async def toggle_tripcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_config(context).get("identity.allow_tripcode_toggle", True):
        await update.effective_message.reply_text("Persistent tripcode is disabled by config.")
        return
    await _toggle(update, context, "tripcode_enabled", "Persistent tripcode")


async def set_tripcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if not user:
        return
    raw = args_text(context)
    if "#" not in raw:
        await update.effective_message.reply_text("Use /settripcode name#secret")
        return
    name, secret = raw.split("#", 1)
    try:
        name, code = make_tripcode(name, secret, str(get_config(context).get("bot.global_salt", "")))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    get_repo(context).set_tripcode(user.telegram_id, name, code, True)
    await update.effective_message.reply_html(f"Tripcode set: <b>{html_escape(name)}</b> !{html_escape(code)}")


async def unset_tripcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if user:
        get_repo(context).set_tripcode(user.telegram_id, None, None, False)
    await update.effective_message.reply_text("Tripcode cleared.")


async def signed_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = args_text(context)
    if not text:
        await update.effective_message.reply_text("Use /s <message>")
        return
    from forward_bot.handlers.message_handlers import submit_text
    await submit_text(update, context, text, identity_mode="signed")


async def tripcode_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = args_text(context)
    user, _ = await ensure_user(update, context)
    if not text:
        await update.effective_message.reply_text("Use /t <message>")
        return
    if not user or not user.tripcode_name or not user.tripcode_hash:
        await update.effective_message.reply_text("Set a tripcode first with /settripcode name#secret.")
        return
    from forward_bot.handlers.message_handlers import submit_text
    await submit_text(update, context, text, identity_mode="tripcode")


async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    target, error, _ = await resolve_target_user(update, context, user)
    if not user or not target:
        await command_reply(update, context, error or "No target.")
        return
    if target.telegram_id == user.telegram_id:
        await command_reply(update, context, "You cannot block yourself.")
        return
    repo = get_repo(context)
    repo.add_block(user.telegram_id, target.telegram_id)
    touch_activity(context, user.telegram_id)
    suffix = "\nModeration visibility is preserved for mods/admins." if user.is_mod_or_admin else ""
    await command_reply(update, context, "Sender blocked." + suffix)


async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    repo = get_repo(context)
    removed = repo.remove_latest_block(user.telegram_id) if user else None
    if user:
        touch_activity(context, user.telegram_id)
    await update.effective_message.reply_text("Most recent block removed." if removed else "You have no blocked users.")


async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    repo = get_repo(context)
    config = get_config(context)
    if not user:
        return
    target = None
    amount = None
    if update.effective_message.reply_to_message and len(context.args or []) == 1:
        target_id, error = await resolve_replied_sender(update, context)
        target = repo.get_user(target_id) if target_id else None
        amount = _parse_amount(context.args[0])
    elif len(context.args or []) >= 2:
        target = resolve_user_reference(repo, config, context.args[0], user)
        amount = _parse_amount(context.args[1])
    if not target or amount is None:
        await command_reply(update, context, "Use /credit <amount> in reply, or /credit <user> <amount>.")
        return
    amount = round_credits(amount)
    if amount == 0:
        await command_reply(update, context, "Amount must be non-zero.")
        return
    if target.telegram_id == user.telegram_id:
        await command_reply(update, context, "You cannot transfer to yourself.")
        return
    if not user.is_admin:
        if amount <= 0:
            await command_reply(update, context, "Normal users can only send positive amounts.")
            return
        if user.credits < amount:
            await command_reply(update, context, "Insufficient credits.")
            return
        sender, target = repo.transfer_credits(user.telegram_id, target.telegram_id, amount)
        touch_activity(context, user.telegram_id)
        await command_reply(update, context, f"Sent {amount:.2f} credits to {display_identity_html(target, config, viewer=user)}. Balance: {sender.credits:.2f}", parse_mode="HTML")
        try:
            await context.bot.send_message(target.telegram_id, f"You received {amount:.2f} credits.", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
        except TelegramError as exc:
            log_telegram_error(LOGGER, "credit.notify_target", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=target.telegram_id)
            pass
    else:
        applied, target = repo.apply_credit_change(target.telegram_id, amount, "admin_adjustment", daily_caps=None)
        maybe_apply_negative_cooldown(repo, config, target)
        await command_reply(update, context, f"Adjusted {display_identity_html(target, config, viewer=user)} by {applied:.2f}. Balance: {target.credits:.2f}", parse_mode="HTML")
        try:
            await context.bot.send_message(target.telegram_id, f"Admin credit adjustment: {applied:.2f}. Balance: {target.credits:.2f}", reply_to_message_id=await reply_to_for_target(update, context, target.telegram_id))
        except TelegramError as exc:
            log_telegram_error(LOGGER, "credit.admin_notify_target", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=target.telegram_id)
            pass


def _parse_amount(value: str) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


async def creditstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    repo = get_repo(context)
    config = get_config(context)
    values = repo.credit_values(started_only=True)
    mn, med, mx = mean_median(values)
    lines = ["<b>Credit Stats</b>", "", "<b>Leaderboards</b>", "Current balance:"]
    for i, top in enumerate(repo.top_current_credits(10), 1):
        lines.append(f"{i}. {display_identity_html(top, config, viewer=user)}: {top.credits:.2f}")
    daily = repo.top_daily_earners(10)
    lines.append("")
    lines.append("Today's earners:")
    lines.extend(
        [f"{i}. {display_identity_html(top, config, viewer=user)}: +{earned:.2f}" for i, (top, earned) in enumerate(daily, 1)]
        or ["No earnings recorded today."]
    )
    caller = user.credits if user else 0
    tr = tax_rate(config, caller)
    lr = loss_rate(config, caller)
    caps = daily_caps(config)
    downvote_schedule = config.get("credits.downvote_cost_schedule", []) or []
    loss_schedule = config.get("loss_rate.schedule", []) or []
    lines.extend([
        "",
        "<b>Economy</b>",
        f"Daily net issuance: {repo.net_issuance_since_days(1):.2f}",
        f"Weekly net issuance: {repo.net_issuance_since_days(7):.2f}",
        f"Started-user balances: min {mn:.2f}, median {med:.2f}, max {mx:.2f}",
        "",
        "<b>Your Account</b>",
        f"Balance: {caller:.2f}",
        f"Daily tax rate: {tr * 100:.2f}%",
        f"Loss rate: {lr * 100:.2f}%",
        f"Expected balance after one tax day: {caller - caller * tr:.2f}",
        "",
        "<b>Rewards</b>",
        f"Text message: +{float(config.get('credits.text_message_reward', 0) or 0):.2f}",
        f"Media message: +{float(config.get('credits.media_message_reward', 0) or 0):.2f}",
        f"Upvote received: +{float(config.get('credits.upvote_reward', 0) or 0):.2f}",
        f"Invite redeemed: +{float(config.get('credits.invite_reward', 0) or 0):.2f}",
        "",
        "<b>Costs and Penalties</b>",
        f"Starting balance: {float(config.get('credits.starting_balance', 0) or 0):.2f}",
        f"Upvote sent: -{float(config.get('credits.upvote_cost', 0) or 0):.2f}",
        f"Downvote penalty to sender: -{float(config.get('credits.downvote_penalty', 0) or 0):.2f}",
        f"Unsend: -{float(config.get('credits.unsend_cost', 0) or 0):.2f}",
        f"Edit: -{float(config.get('credits.edit_cost', 0) or 0):.2f}",
        f"Whisper: -{float(config.get('credits.whisper_cost', 0) or 0):.2f}",
        f"Fight fee: {float(config.get('fights.initiation_fee_percent', 0) or 0) * 100:.2f}% of stake, min {float(config.get('fights.initiation_fee_min', 0) or 0):.2f}, max {float(config.get('fights.initiation_fee_max', 0) or 0):.2f}",
        f"Fight win tax: {float(config.get('fights.win_tax_percent', 0) or 0) * 100:.2f}%",
        "",
        "<b>Daily Earning Caps</b>",
    ])
    lines.extend([f"{_credit_reason_label(reason)}: {'unlimited' if cap < 0 else f'{cap:.2f}'}" for reason, cap in sorted(caps.items())] or ["No caps configured."])
    lines.append("")
    lines.append("<b>Downvote Cost Schedule</b>")
    lines.extend([f"After {item.get('minute')} minute(s): {float(item.get('cost', 0)):.2f}" for item in downvote_schedule] or ["No schedule configured."])
    lines.append("")
    lines.append("<b>Loss Rate Schedule</b>")
    lines.extend([f"At {float(item.get('credits', 0)):.2f} credits: {float(item.get('loss_rate', 0)) * 100:.2f}%" for item in loss_schedule] or ["No schedule configured."])
    await update.effective_message.reply_html("\n".join(lines))


def _credit_reason_label(reason: str) -> str:
    labels = {
        "text_message_reward": "Text messages",
        "media_message_reward": "Media messages",
        "upvote_reward": "Upvotes received",
        "invite_reward": "Invite rewards",
        "gamble_win": "Gamble winnings",
        "fight_win": "Fight winnings",
        "admin_adjustment": "Admin adjustments",
        "daily_tax": "Daily tax",
        "upvote_cost": "Upvotes sent",
        "downvote_cost": "Downvotes sent",
        "downvote_penalty": "Downvotes received",
        "whisper_cost": "Whispers",
        "edit_cost": "Edits",
        "unsend_cost": "Unsend",
        "fight_fee": "Fight fees",
        "fight_loss": "Fight losses",
        "gamble_loss": "Gamble losses",
    }
    return labels.get(reason, reason.replace("_", " ").strip().capitalize())


async def gamble(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if not user or not context.args:
        await update.effective_message.reply_text("Use /gamble <amount>")
        return
    amount = _parse_amount(context.args[0])
    max_amount = float(get_config(context).get("gamble.max_amount", 1000) or 1000)
    if amount is None or amount < 0.01 or amount > user.credits or amount > max_amount:
        await update.effective_message.reply_text("Invalid gamble amount.")
        return
    repo = get_repo(context)
    config = get_config(context)
    if random.random() < 0.5:
        applied, updated = apply_credit(repo, config, user.telegram_id, amount, "gamble_win")
        result = f"Won {applied:.2f}"
    else:
        applied, updated = apply_credit(repo, config, user.telegram_id, -amount, "gamble_loss", cap_positive=False)
        result = f"Lost {abs(applied):.2f}"
    touch_activity(context, user.telegram_id)
    await update.effective_message.reply_text(f"{result}. Balance: {updated.credits:.2f}")


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if not user:
        return
    repo = get_repo(context)
    config = get_config(context)
    code = repo.get_invite_for_user(user.telegram_id)
    if not code:
        prefix = str(config.get("invites.start_prefix", "inv_"))
        while True:
            code = prefix + random_token(10)
            if not repo.get_invite(code):
                break
        repo.create_invite(user.telegram_id, code)
    touch_activity(context, user.telegram_id)
    try:
        me = await context.bot.get_me()
        text = f"https://t.me/{me.username}?start={code}" if me.username else code
    except TelegramError as exc:
        log_telegram_error(LOGGER, "invite.get_me", exc, aggregate=context.application.bot_data.get("aggregate_logger"), user_id=user.telegram_id)
        text = code
    await update.effective_message.reply_text(text)


async def sendinvite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = args_text(context)
    if not re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/", text, re.I):
        await update.effective_message.reply_text("Use /sendinvite <Telegram invite link> <description>")
        return
    without_links = re.sub(r"https?://\S+|(?:t\.me|telegram\.me)/\S+", "", text, flags=re.I)
    if len(re.sub(r"\W+", "", without_links)) < 3:
        await update.effective_message.reply_text("Add a meaningful description to the invite link.")
        return
    from forward_bot.handlers.message_handlers import submit_text
    await submit_text(update, context, text, force_remove_buttons=True)


async def unsend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    msg, _, error = await resolve_message_from_reply(update, context)
    if not user or not msg:
        await command_reply(update, context, error or "No message.")
        return
    if msg.sender_id != user.telegram_id:
        await command_reply(update, context, "You can only unsend your own messages.")
        return
    cost = float(get_config(context).get("credits.unsend_cost", 5) or 5)
    if user.credits < cost and not user.is_admin:
        await command_reply(update, context, "Insufficient credits.")
        return
    repo = get_repo(context)
    if not user.is_admin:
        apply_credit(repo, get_config(context), user.telegram_id, -cost, "unsend_cost", cap_positive=False)
    count = await remove_message(context.bot, repo, get_store(context), get_config(context), msg.id, reason="unsent by sender", notify_sender=False, remove_for_mods=True)
    updated = repo.get_user(user.telegram_id)
    await command_reply(update, context, f"Unsent. Removed {count} copies. Cost: {cost:.2f}. Balance: {updated.credits:.2f}.")


async def deletevote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    msg, _, error = await resolve_message_from_reply(update, context)
    if not user or not msg:
        await command_reply(update, context, error or "No message.")
        return
    ok, text = await vote_to_remove(context.bot, get_repo(context), get_store(context), get_config(context), msg.id, user)
    await command_reply(update, context, text)


async def whisper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    if not user:
        return
    raw = args_text(context)
    repo = get_repo(context)
    config = get_config(context)
    store = get_store(context)
    target = None
    text = raw
    reply_to_message_id = None
    reply_to_whisper_id = None
    if update.effective_message.reply_to_message:
        target_id, error = await resolve_replied_sender(update, context)
        target = repo.get_user(target_id) if target_id else None
        text = raw
        normal_msg, _, _ = await resolve_message_from_reply(update, context)
        if normal_msg:
            reply_to_message_id = normal_msg.id
        wdel = store.resolve_whisper_delivery(user.telegram_id, update.effective_message.reply_to_message.message_id)
        if wdel:
            reply_to_whisper_id = wdel.whisper_id
    elif len(context.args or []) >= 2:
        target = resolve_user_reference(repo, config, context.args[0], user)
        text = " ".join(context.args[1:])
    if not target or not text:
        await command_reply(update, context, "Use /w in reply <text>, or /w <user> <text>.")
        return
    await _send_whisper(update, context, user, target, text, reply_to_message_id=reply_to_message_id, reply_to_whisper_id=reply_to_whisper_id)


async def _send_whisper(update: Update, context: ContextTypes.DEFAULT_TYPE, user, target, text: str, *, reply_to_message_id=None, reply_to_whisper_id=None, modwhisper=False) -> None:
    repo = get_repo(context)
    config = get_config(context)
    store = get_store(context)
    if target.telegram_id == user.telegram_id:
        await command_reply(update, context, "You cannot whisper yourself.")
        return
    cost = 0.0 if user.is_mod_or_admin or modwhisper else float(config.get("credits.whisper_cost", 1) or 1)
    unlock = float(config.get("credits.whisper_unlock_credits", 30) or 30)
    if not user.is_mod_or_admin and not modwhisper and user.credits < unlock:
        await command_reply(update, context, "You need more credits to unlock whispers.")
        return
    if user.credits < cost:
        await command_reply(update, context, "Insufficient credits.")
        return
    identity_mode = None
    if text.startswith("/s "):
        identity_mode, text = "signed", text[3:]
    elif text.startswith("/t "):
        identity_mode, text = "tripcode", text[3:]
    elif user.sign_enabled:
        identity_mode = "signed"
    elif user.tripcode_enabled:
        identity_mode = "tripcode"
    prefix = "Modwhisper" if modwhisper else "Whisper"
    suffix_html = ""
    if identity_mode == "signed":
        suffix = f"~ @{user.username}" if user.username else "~ signed"
        suffix_html = f"<i>{html_escape(suffix)}</i>"
    elif identity_mode == "tripcode":
        if not user.tripcode_name or not user.tripcode_hash:
            await command_reply(update, context, "Set a tripcode first with /settripcode name#secret.")
            return
        suffix_html = f"<b>{html_escape(user.tripcode_name)}</b> !{html_escape(user.tripcode_hash)}"
    if identity_mode == "tripcode":
        body = f"<i><b>{prefix}:</b></i> {suffix_html}:\n{html_escape(text)}"
    elif suffix_html:
        body = f"<i><b>{prefix}:</b></i> {html_escape(text)}\n\n{suffix_html}"
    else:
        body = f"<i><b>{prefix}:</b></i> {html_escape(text)}"
    reply_to = None
    if reply_to_message_id:
        prior_msg = store.get_message(reply_to_message_id)
        if prior_msg and prior_msg.sender_id == target.telegram_id and prior_msg.source_chat_id == target.telegram_id:
            reply_to = prior_msg.source_message_id
        else:
            reply_to = store.delivery_reply_for_recipient(reply_to_message_id, target.telegram_id)
    elif reply_to_whisper_id:
        priorw = next((d for d in store.deliveries_for_whisper(reply_to_whisper_id) if d.recipient_id == target.telegram_id), None)
        reply_to = priorw.telegram_message_id if priorw else None
    if (reply_to_message_id or reply_to_whisper_id) and not reply_to:
        await command_reply(update, context, "Reply target is not available for the recipient.")
        return
    try:
        sent = await context.bot.send_message(target.telegram_id, body, parse_mode="HTML", reply_to_message_id=reply_to)
    except TelegramError as exc:
        log_telegram_error(LOGGER, "whisper.send_target", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=target.telegram_id)
        await command_reply(update, context, "Recipient unavailable.")
        return
    whisper_obj = store.add_whisper(sender_id=user.telegram_id, target_id=target.telegram_id, text=body, is_modwhisper=modwhisper, reply_to_message_id=reply_to_message_id, reply_to_whisper_id=reply_to_whisper_id)
    if update.effective_message:
        store.add_whisper_delivery(whisper_obj.id, user.telegram_id, update.effective_message.message_id)
    store.add_whisper_delivery(whisper_obj.id, target.telegram_id, sent.message_id)
    for mod in repo.list_users():
        if mod.is_mod_or_admin and mod.has_started and mod.telegram_id not in {user.telegram_id, target.telegram_id}:
            try:
                mirror_reply_to = _whisper_mirror_reply_to(store, mod.telegram_id, reply_to_message_id, reply_to_whisper_id)
                mirror = await context.bot.send_message(mod.telegram_id, body, parse_mode="HTML", reply_to_message_id=mirror_reply_to)
                store.add_whisper_delivery(whisper_obj.id, mod.telegram_id, mirror.message_id)
            except TelegramError as exc:
                log_telegram_error(LOGGER, "whisper.send_mirror", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=mod.telegram_id)
                pass
    if cost:
        _, user = apply_credit(repo, config, user.telegram_id, -cost, "whisper_cost", cap_positive=False)
    touch_activity(context, user.telegram_id)
    await command_reply(update, context, f"Whisper sent. Cost: {cost:.2f}. Balance: {user.credits:.2f}")


def _whisper_mirror_reply_to(store, recipient_id: int, reply_to_message_id: int | None, reply_to_whisper_id: int | None) -> int | None:
    if reply_to_message_id:
        prior_msg = store.get_message(reply_to_message_id)
        if prior_msg and prior_msg.sender_id == recipient_id and prior_msg.source_chat_id == recipient_id:
            return prior_msg.source_message_id
        return store.delivery_reply_for_recipient(reply_to_message_id, recipient_id)
    if reply_to_whisper_id:
        delivery = next((d for d in store.deliveries_for_whisper(reply_to_whisper_id) if d.recipient_id == recipient_id), None)
        return delivery.telegram_message_id if delivery else None
    return None


async def wmods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    text = args_text(context)
    if not user or not text:
        await update.effective_message.reply_text("Use /wmods <text>")
        return
    repo = get_repo(context)
    store = get_store(context)
    body = f"<i><b>Modwhisper:</b></i> {html_escape(text)}"
    whisper_obj = store.add_whisper(sender_id=user.telegram_id, target_id=0, text=body, is_modwhisper=True)
    if update.effective_message:
        store.add_whisper_delivery(whisper_obj.id, user.telegram_id, update.effective_message.message_id)
    sent_count = 0
    for target in repo.list_users():
        if target.is_mod_or_admin and target.has_started and target.telegram_id != user.telegram_id:
            try:
                msg = await context.bot.send_message(target.telegram_id, body, parse_mode="HTML")
                store.add_whisper_delivery(whisper_obj.id, target.telegram_id, msg.message_id)
                sent_count += 1
            except TelegramError as exc:
                log_telegram_error(LOGGER, "wmods.send", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=target.telegram_id)
                pass
    touch_activity(context, user.telegram_id)
    await update.effective_message.reply_text(f"Message sent ({sent_count} mod copies).")


async def sauce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    config = get_config(context)
    store = get_store(context)
    if not config.get("saucenao.enabled", False) or not config.get("saucenao.api_key") or str(config.get("saucenao.api_key")).startswith("${"):
        await command_reply(update, context, "SauceNAO is not configured.")
        return
    msg, delivery, error = await resolve_message_from_reply(update, context)
    if not user or not msg:
        await command_reply(update, context, error or "No message.")
        return
    if delivery and delivery.blurred:
        await command_reply(update, context, "Cannot search blurred media.")
        return
    cached = store.get_sauce_cache(msg.id)
    if cached:
        await command_reply(update, context, cached + "\n(cached)\n" + _sauce_remaining_text(store, config, user.telegram_id))
        return
    user_used, global_used = store.get_sauce_usage(user.telegram_id)
    per_user = int(config.get("saucenao.per_user_daily_limit", -1) or -1)
    global_limit = int(config.get("saucenao.global_daily_limit", -1) or -1)
    if per_user >= 0 and user_used >= per_user:
        await command_reply(update, context, "Daily SauceNAO user limit reached.")
        return
    if global_limit >= 0 and global_used >= global_limit:
        await command_reply(update, context, "Daily SauceNAO global limit reached.")
        return
    gate = float(config.get("saucenao.top_credit_percentile", 0) or 0)
    if gate > 0:
        cutoff = get_repo(context).credit_percentile_cutoff(gate)
        if user.credits < cutoff:
            await command_reply(update, context, "You need enough credits to use /sauce.")
            return
    file_id = msg.thumbnail_file_id if msg.content_type in {"video", "animation", "video_note", "sticker"} else msg.media_file_id
    file_id = file_id or msg.media_file_id or msg.thumbnail_file_id
    if not file_id:
        await command_reply(update, context, "No searchable media.")
        return
    user_used, global_used = store.record_sauce_usage(user.telegram_id)
    try:
        tg_file = await context.bot.get_file(file_id)
        url = tg_file.file_path
        import aiohttp
        params = {"api_key": config.get("saucenao.api_key"), "url": url, "numres": int(config.get("saucenao.num_results", 6) or 6), "output_type": 2}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://saucenao.com/search.php", params=params, timeout=20) as resp:
                data = await resp.json()
        result = _format_sauce(data)
    except Exception:
        await command_reply(update, context, "Lookup failed.")
        return
    store.add_sauce_cache(msg.id, result)
    await command_reply(update, context, result + "\n" + _sauce_remaining_text(store, config, user.telegram_id, user_used=user_used, global_used=global_used))


def _format_sauce(data) -> str:
    results = data.get("results") or []
    if not results:
        return "No results."
    top = results[0]
    header = top.get("header", {})
    item = top.get("data", {})
    urls = item.get("ext_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    lines = [
        f"Title: {item.get('title') or item.get('material') or 'unknown'}",
        f"Similarity: {header.get('similarity', '?')}%",
    ]
    if item.get("author_name") or item.get("member_name"):
        lines.append(f"Author: {item.get('author_name') or item.get('member_name')}")
    lines.extend(urls[:3])
    return "\n".join(lines)


def _sauce_remaining_text(store, config, user_id: int, *, user_used: int | None = None, global_used: int | None = None) -> str:
    if user_used is None or global_used is None:
        user_used, global_used = store.get_sauce_usage(user_id)
    per_user = int(config.get("saucenao.per_user_daily_limit", -1) or -1)
    global_limit = int(config.get("saucenao.global_daily_limit", -1) or -1)
    user_remaining = "unlimited" if per_user < 0 else str(max(0, per_user - user_used))
    global_remaining = "unlimited" if global_limit < 0 else str(max(0, global_limit - global_used))
    return f"Remaining today: user {user_remaining}, global {global_remaining}."


async def fight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, _ = await ensure_user(update, context)
    config = get_config(context)
    repo = get_repo(context)
    store = get_store(context)
    if not user or not config.get("fights.enabled", True):
        await command_reply(update, context, "Fights are disabled.")
        return
    cooldown_left = store.latest_fight_request_seconds_left(user.telegram_id, int(config.get("fights.cooldown_seconds", 300) or 300))
    if cooldown_left > 0:
        await command_reply(update, context, f"Fight cooldown: {human_seconds(cooldown_left)}.")
        return
    target, _, rest = await resolve_target_user(update, context, user)
    amount_arg = rest.split()[0] if rest else None
    if not target or target.telegram_id == user.telegram_id:
        await command_reply(update, context, "Use /fight in reply or /fight <user> [amount].")
        return
    if not target.fights_enabled:
        await command_reply(update, context, "Target does not accept fights.")
        return
    max_cap = float(config.get("fights.max_stake_cap", 500) or 500)
    if amount_arg:
        try:
            stake = round_credits(float(amount_arg))
        except (TypeError, ValueError):
            await command_reply(update, context, "Use /fight in reply or /fight <user> [amount]. Amount must be numeric.")
            return
    else:
        stake = round_credits(min(max_cap, max(1.0, math.sqrt(max(0.0, user.credits)))))
    if stake < 0.01 or stake > max_cap or user.credits < stake:
        await command_reply(update, context, "Invalid stake.")
        return
    fee = round_credits(min(float(config.get("fights.initiation_fee_max", 20) or 20), max(float(config.get("fights.initiation_fee_min", 1) or 1), stake * float(config.get("fights.initiation_fee_percent", 0.05) or 0.05))))
    if user.credits < stake + fee:
        await command_reply(update, context, "Insufficient credits for stake plus fee.")
        return
    tier_diff = math.floor(math.log2(max(1.0, user.credits))) - math.floor(math.log2(max(1.0, target.credits)))
    matchup = "even"
    if tier_diff >= 2: matchup = "advantage"
    elif tier_diff == 1: matchup = "slight advantage"
    elif tier_diff == -1: matchup = "slight disadvantage"
    elif tier_diff <= -2: matchup = "disadvantage"
    timeout = int(config.get("fights.request_timeout_seconds", 300) or 300)
    from datetime import timedelta
    from forward_bot.utils import now_utc
    fight_req = store.add_fight(sender_id=user.telegram_id, target_id=target.telegram_id, stake=stake, fee=fee, matchup=matchup, command_message_id=update.effective_message.message_id, expires_at=now_utc() + timedelta(seconds=timeout))
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Accept", callback_data=f"facc:{fight_req.id}"),
        InlineKeyboardButton("Decline", callback_data=f"fdec:{fight_req.id}"),
    ]])
    reply_to = await reply_to_for_target(update, context, target.telegram_id)
    if update.effective_message.reply_to_message and not reply_to:
        store.fights.pop(fight_req.id, None)
        await command_reply(update, context, "Reply target is not available for the recipient.")
        return
    try:
        sent = await context.bot.send_message(target.telegram_id, f"Fight request: stake {stake:.2f}, matchup {matchup}.", reply_to_message_id=reply_to, reply_markup=markup)
    except TelegramError as exc:
        log_telegram_error(LOGGER, "fight.send_request", exc, aggregate=context.application.bot_data.get("aggregate_logger"), repo=repo, user_id=target.telegram_id)
        store.fights.pop(fight_req.id, None)
        await command_reply(update, context, "Recipient unavailable.")
        return
    fight_req.target_message_id = sent.message_id
    _, updated = apply_credit(repo, config, user.telegram_id, -fee, "fight_fee", cap_positive=False)
    touch_activity(context, user.telegram_id)
    balance = updated.credits if updated else user.credits - fee
    await command_reply(update, context, f"Fight sent. Fee: {fee:.2f}. Balance: {balance:.2f}.")
