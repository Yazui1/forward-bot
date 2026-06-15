from __future__ import annotations

import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from forward_bot.crypto.obfuscation import temporal_id
from forward_bot.messages import Messages as Msg

logger = logging.getLogger(__name__)


def tombstone(reason: str | None = None) -> str:
    return Msg.TOMBSTONE


def _identity_for_viewer(user: Any | None, user_id: int, viewer: Any, salt: str) -> str:
    anon = temporal_id(user_id, salt)
    if user is None:
        return anon
    trip = (
        f"<b>{html.escape(str(user.tripcode_name))}</b> !{str(user.tripcode_hash)[:6]}"
        if user.tripcode_name and user.tripcode_hash
        else None
    )
    if viewer.is_admin:
        parts = [anon]
        if trip:
            parts.append(trip)
        if user.username:
            parts.append(f"@{html.escape(user.username)}")
        return " ".join(parts)
    return trip or anon


async def remove_message_with_tombstones(
    context: Any,
    repo: Any,
    cfg: dict[str, Any],
    message_id: int,
    sender_id: int,
    reason: str,
    notify_mods: bool = True,
    notify_sender: bool = True,
) -> None:
    first_mod_note_message_id = None

    if notify_mods:
        first_mod_note_message_id = await _send_moderation_notes(context, repo, cfg, message_id, sender_id, reason)
        if first_mod_note_message_id is not None:
            await repo.set_message_tombstone_mod_message(message_id, first_mod_note_message_id)

    message = await repo.get_message(message_id)
    if notify_sender and message is not None and message.get("source_chat_id") and message.get("source_message_id"):
        source_chat_id = int(message["source_chat_id"])
        source_message_id = int(message["source_message_id"])
        await _notify_sender_pending_moderation(
            context,
            source_chat_id,
            source_message_id,
            reason,
        )

    deliveries = await repo.list_deliveries_for_message(message_id)
    for delivery in deliveries:
        recipient_id = int(delivery["recipient_id"])
        telegram_message_id = int(delivery["telegram_message_id"])
        recipient = await repo.get_user(recipient_id)
        is_mod = bool(recipient and (
            recipient.is_moderator or recipient.is_admin))
        if is_mod:
            continue
        text = tombstone(reason)
        tombstone_message_id, tombstone_kind = await _replace_or_send_tombstone(
            context,
            recipient_id,
            telegram_message_id,
            text,
            str(message.get("content_type") or "") if message is not None else "",
            preserve_reference=False,
        )
        await repo.mark_delivery_tombstoned(int(delivery["id"]), tombstone_message_id, tombstone_kind)

    await update_message_for_mods(
        context,
        repo,
        message_id,
        "This message was removed and is pending moderation action",
    )
    await repo.set_message_deleted(message_id, reason)
    await repo.add_audit_event("message_removed", target_user_id=sender_id, message_id=message_id, details=reason)


async def _notify_sender_pending_moderation(context: Any, chat_id: int, message_id: int, reason: str) -> None:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=Msg.removed_pending(reason),
            reply_to_message_id=message_id,
        )
    except Exception as exc:
        logger.debug(
            "Failed to notify removed-message sender chat_id=%s message_id=%s: %s", chat_id, message_id, exc)


async def remove_message_for_mods(
    context: Any,
    repo: Any,
    cfg: dict[str, Any],
    message_id: int,
    sender_id: int,
    reason: str,
) -> int:
    salt = cfg["bot"]["global_salt"]
    deliveries = await repo.list_deliveries_for_message(message_id)
    message = await repo.get_message(message_id)
    content_type = str((message or {}).get("content_type") or "")
    removed = 0
    for delivery in deliveries:
        recipient_id = int(delivery["recipient_id"])
        telegram_message_id = int(delivery["telegram_message_id"])
        recipient = await repo.get_user(recipient_id)
        is_mod = bool(recipient and (
            recipient.is_moderator or recipient.is_admin))
        if not is_mod:
            continue
        text = tombstone(reason)
        tombstone_message_id, tombstone_kind = await _replace_or_send_tombstone(
            context,
            recipient_id,
            telegram_message_id,
            text,
            content_type,
            preserve_reference=True,
        )
        await repo.mark_delivery_tombstoned(int(delivery["id"]), tombstone_message_id, tombstone_kind)
        removed += 1
    await repo.add_audit_event("message_removed_for_mods", target_user_id=sender_id, message_id=message_id, details=reason)
    return removed


async def append_action_info_to_message_for_mods(
    context: Any,
    repo: Any,
    message_id: int,
    action_info: str,
) -> int:
    message = await repo.get_message(message_id)
    if message is None:
        return 0
    base = str(message.get("text_content") or "")
    if base.endswith(action_info):
        return 0
    separator = "\n\n" if base else ""
    updated_text = f"{base}{separator}{action_info}"
    await repo.update_message_text_content(message_id, updated_text)
    return await update_message_for_mods(context, repo, message_id, None, reaction_status=action_info)


async def update_message_for_mods(
    context: Any,
    repo: Any,
    message_id: int,
    status: str | None,
    reaction_status: str | None = None,
) -> int:
    message = await repo.get_message(message_id)
    if message is None:
        return 0
    deliveries = await repo.list_deliveries_for_message(message_id)
    updated = 0
    text_status = f"\n\n<b><i>{html.escape(status)}</i></b>" if status else ""
    plain_status = f"\n\n{status}" if status else ""
    for delivery in deliveries:
        recipient_id = int(delivery["recipient_id"])
        telegram_message_id = int(delivery["telegram_message_id"])
        recipient = await repo.get_user(recipient_id)
        is_mod = bool(recipient and (
            recipient.is_moderator or recipient.is_admin))
        if not is_mod:
            continue
        content_type = str(message.get("content_type") or "")
        base = str(message.get("text_content") or "")
        base_html = base if str(message.get(
            "parse_mode") or "").upper() == "HTML" else html.escape(base)
        try:
            if content_type == "text":
                await context.bot.edit_message_text(
                    chat_id=recipient_id,
                    message_id=telegram_message_id,
                    text=f"{base_html}{text_status}" if base_html else (
                        text_status.lstrip() or ""),
                    parse_mode="HTML",
                )
            else:
                await context.bot.edit_message_caption(
                    chat_id=recipient_id,
                    message_id=telegram_message_id,
                    caption=f"{base_html}{text_status}" if base_html else (
                        text_status.lstrip() or ""),
                    parse_mode="HTML",
                )
            updated += 1
        except Exception as exc:
            logger.debug(
                "HTML status append failed message_id=%s recipient_id=%s telegram_message_id=%s content_type=%s reason=%s detail=%s",
                message_id,
                recipient_id,
                telegram_message_id,
                content_type,
                _telegram_edit_failure_reason(exc),
                exc,
            )
            try:
                if content_type == "text":
                    await context.bot.edit_message_text(
                        chat_id=recipient_id,
                        message_id=telegram_message_id,
                        text=f"{base}{plain_status}" if base else (
                            status or ""),
                    )
                else:
                    await context.bot.edit_message_caption(
                        chat_id=recipient_id,
                        message_id=telegram_message_id,
                        caption=f"{base}{plain_status}" if base else (
                            status or ""),
                    )
                updated += 1
            except Exception as fallback_exc:
                logger.debug(
                    "Failed to append mod-visible message status message_id=%s recipient_id=%s telegram_message_id=%s content_type=%s reason=%s detail=%s",
                    message_id,
                    recipient_id,
                    telegram_message_id,
                    content_type,
                    _telegram_edit_failure_reason(fallback_exc),
                    fallback_exc,
                )
                if await _set_mod_status_reaction(
                    context,
                    recipient_id,
                    telegram_message_id,
                    reaction_status or status,
                ):
                    updated += 1
    return updated


def _telegram_edit_failure_reason(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, BadRequest):
        if "message can't be edited" in text or "message can not be edited" in text:
            return "telegram says this message cannot be edited; common causes are media without editable caption, protected/forwarded messages, old messages, or unsupported message type"
        if "message is not modified" in text:
            return "message already has the requested content"
        if "message to edit not found" in text:
            return "message no longer exists or is no longer visible to the bot"
    return type(exc).__name__


async def _set_mod_status_reaction(
    context: Any,
    chat_id: int,
    message_id: int,
    status: str | None,
) -> bool:
    emoji = _reaction_for_status(context, status)
    if not emoji:
        return False
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[emoji],
        )
        logger.debug(
            "Applied mod-visible status reaction chat_id=%s message_id=%s emoji=%s status=%s",
            chat_id,
            message_id,
            emoji,
            status,
        )
        return True
    except Exception as exc:
        logger.debug(
            "Failed to apply mod-visible status reaction chat_id=%s message_id=%s emoji=%s reason=%s detail=%s",
            chat_id,
            message_id,
            emoji,
            type(exc).__name__,
            exc,
        )
        return False


def _reaction_for_status(context: Any, status: str | None) -> str | None:
    cfg = getattr(getattr(context, "application", None), "bot_data", {}).get("cfg", {})
    reactions = cfg.get("moderation", {}).get("status_reactions", {})
    text = (status or "").lower()
    if "confirmed" in text:
        return str(reactions.get("confirmed", "👍"))
    if "removed" in text and "pending" not in text:
        return str(reactions.get("removed", "👌"))
    if "revert" in text or "did not confirm" in text:
        return str(reactions.get("reverted", "👎"))
    if "pending" in text or "moderation action" in text:
        return str(reactions.get("pending", "🤔"))
    return None


async def _replace_or_send_tombstone(
    context: Any,
    chat_id: int,
    message_id: int,
    text: str,
    content_type: str,
    preserve_reference: bool = False,
) -> tuple[int | None, str]:
    if content_type == "text":
        try:
            edited = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
            logger.debug("Edited tombstone chat_id=%s message_id=%s",
                         chat_id, message_id)
            return edited.message_id, "edited"
        except Exception as exc:
            logger.debug(
                "Edit tombstone failed chat_id=%s message_id=%s: %s", chat_id, message_id, exc)

    if content_type != "text" and preserve_reference:
        logger.debug(
            "Delete-for-mods requested for media; media cannot become a text tombstone, deleting original chat_id=%s message_id=%s content_type=%s",
            chat_id,
            message_id,
            content_type,
        )

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(
            "Deleted message before tombstone chat_id=%s message_id=%s", chat_id, message_id)
        if content_type != "text":
            return None, "deleted_uneditable_media"
    except Exception as exc:
        logger.debug(
            "Delete before tombstone failed chat_id=%s message_id=%s: %s", chat_id, message_id, exc)

    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        logger.debug("Sent tombstone chat_id=%s original_message_id=%s tombstone_id=%s",
                     chat_id, message_id, sent.message_id)
        return sent.message_id, "sent"
    except Exception as exc:
        logger.warning(
            "Failed to send tombstone chat_id=%s message_id=%s: %s", chat_id, message_id, exc)
        return None, "failed"


async def _send_moderation_notes(
    context: Any,
    repo: Any,
    cfg: dict[str, Any],
    message_id: int,
    sender_id: int,
    reason: str,
) -> int | None:
    mods = await repo.list_mod_and_admin_users()
    salt = cfg["bot"]["global_salt"]
    first_message_id = None
    for mod in mods:
        try:
            text, buttons = await moderation_note_text_and_markup(repo, cfg, message_id, sender_id, reason, mod)
            reply_to = await repo.delivery_message_for_recipient(message_id, mod.telegram_id)
            if reply_to is None:
                reply_to = await repo.delivery_or_tombstone_message_for_recipient(message_id, mod.telegram_id)
            sent = await context.bot.send_message(
                chat_id=mod.telegram_id,
                text=text,
                reply_markup=buttons,
                reply_to_message_id=reply_to,
                parse_mode="HTML",
            )
            if first_message_id is None:
                first_message_id = sent.message_id
            await repo.add_moderation_note(
                message_id=message_id,
                sender_id=sender_id,
                moderator_id=mod.telegram_id,
                telegram_message_id=sent.message_id,
                reason=reason,
                note_type="removal",
            )
        except Exception:
            await repo.add_audit_event(
                "moderation_note_failed",
                target_user_id=mod.telegram_id,
                message_id=message_id,
                details=reason,
            )
    return first_message_id


async def moderation_note_text_and_markup(
    repo: Any,
    cfg: dict[str, Any],
    message_id: int,
    sender_id: int,
    reason: str,
    viewer: Any,
) -> tuple[str, InlineKeyboardMarkup | None]:
    salt = cfg["bot"]["global_salt"]
    sender_user = await repo.get_user(sender_id)
    sender_label = _identity_for_viewer(sender_user, sender_id, viewer, salt)

    voters = await repo.list_remove_voters(message_id)
    voter_lines = []
    for voter_id in voters:
        voter = await repo.get_user(voter_id)
        voter_lines.append(
            f"- {_identity_for_viewer(voter, voter_id, viewer, salt)}")

    message = await repo.get_message(message_id)
    status_lines = []
    headline = Msg.IN_MODERATION_ACTION
    confirmed = bool(message.get("punishment_confirmed")
                     ) if message is not None else False
    reverted = bool(message.get("reverted")) if message is not None else False
    if message is not None:
        if confirmed:
            headline = Msg.CONFIRMED_REMOVAL
        if bool(message.get("removed_for_mods")):
            status_lines.append("Removed for mods: yes")
        if reverted:
            status_lines.append("Reverted: yes")
        if await _has_deleted_uneditable_mod_delivery(repo, message_id):
            status_lines.append(
                "Remove for mods deleted the referenced media because Telegram cannot edit media into a text-only tombstone."
            )

    lines = [
        headline,
        f"Reason: {html.escape(reason)}",
        f"Sender: {sender_label}"
    ]
    if voter_lines:
        lines.append("Voters:")
        lines.extend(voter_lines)
    if status_lines:
        lines.append("")
        lines.extend(status_lines)

    if message is not None and bool(message.get("punishment_confirmed")) and bool(message.get("removed_for_mods")):
        return "\n".join(lines), None

    buttons = []
    row = []
    if not confirmed and not reverted:
        row.append(InlineKeyboardButton(
            "Punish", callback_data=f"mconf:{message_id}:{sender_id}"))
    if not reverted and (message is None or not bool(message.get("removed_for_mods"))):
        row.append(InlineKeyboardButton("Remove for mods",
                   callback_data=f"mrm:{message_id}:{sender_id}"))
    if voter_lines and not reverted and not confirmed:
        row.append(InlineKeyboardButton(
            "Revert", callback_data=f"mrev:{message_id}:{sender_id}"))
    if row:
        buttons.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def _has_deleted_uneditable_mod_delivery(repo: Any, message_id: int) -> bool:
    for delivery in await repo.list_deliveries_for_message(message_id):
        if delivery.get("tombstone_kind") != "deleted_uneditable_media":
            continue
        recipient = await repo.get_user(int(delivery["recipient_id"]))
        if recipient is not None and (recipient.is_moderator or recipient.is_admin):
            return True
    return False


async def refresh_moderation_notes(context: Any, repo: Any, cfg: dict[str, Any], message_id: int, sender_id: int) -> None:
    message = await repo.get_message(message_id)
    reason = str(message.get("deletion_reason")
                 or "removed") if message is not None else "removed"
    notes = await repo.list_moderation_notes_for_message(message_id)
    for note in notes:
        viewer = await repo.get_user(int(note["moderator_id"]))
        if viewer is None:
            continue
        text, markup = await moderation_note_text_and_markup(repo, cfg, message_id, sender_id, reason, viewer)
        try:
            await context.bot.edit_message_text(
                chat_id=int(note["moderator_id"]),
                message_id=int(note["telegram_message_id"]),
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("Failed to refresh moderation note message_id=%s moderator_id=%s: %s",
                         message_id, note["moderator_id"], exc)
