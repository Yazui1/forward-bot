from __future__ import annotations


class Messages:
    ADMIN_ONLY = "Admin only."
    MOD_ONLY = "Moderator/Admin only."
    MESSAGE_NOT_IN_CACHE = "Message is not in cache anymore"
    TARGET_NOT_FOUND = "Target user not found."
    USER_NOT_FOUND = "User not found."
    SENDER_NOT_FOUND = "Sender not found."

    STOPPED = "Stopped. You will not receive forwarded messages."
    USE_START_FIRST = "Use /start first."
    BANNED = "You are banned."
    ABOUT_DEFAULT = "Anonymous message relay bot."
    BLOCKED_REPLY = "Your message was blocked."
    INVITE_LINK_BLOCKED = "Telegram invite links need context. Use /sendinvite <link> <reason/group description>, or send the invite link followed by a description."
    QUESTIONABLE_PROMPT = "This message may be questionable. Send anyway?"
    CONFIRMATION_SEND_BUTTON = "Send anyway"
    RETRY_BUTTON = "Retry"
    RATE_LIMIT_REPLY = "Rate limit hit, send in {seconds} seconds again."
    VOTE_TO_REMOVE_BUTTON = "Vote to remove"
    INACTIVITY_NOTICE = (
        "You are currently inactive, so most non-system messages are not being delivered. "
        "Send meaningful messages or interact normally to become active again. "
        "Abuse such as dotposting or other low-effort activity padding is not allowed."
    )

    INFO_SELF_ONLY = "Normal users can only use /info on themselves."
    CONFIG_RELOADED = "Config reloaded."
    WARNING_ISSUED = "Warning issued."
    MESSAGE_SENT = "Message sent"

    USAGE_TOGGLEMOD = "Usage: /togglemod @user OR reply /togglemod"
    USAGE_BAN = "Usage: /ban @user OR reply /ban"
    USAGE_UNBAN = "Usage: /unban @user|id"
    USAGE_WARN = "Usage: reply /warn OR /warn @user <message>"
    USAGE_COOLDOWN = "Usage: reply /cooldown [dur] OR /cooldown @user <dur>"
    USAGE_UNCOOLDOWN = "Usage: /uncooldown @user|id OR reply /uncooldown"
    USAGE_DELETE = "Reply to a message with /delete."
    USAGE_MODSAY = "Usage: /modsay <message>"
    USAGE_ADMINSAY = "Usage: /adminsay <message>"
    BANNED_NOTIFY = "You have been banned."
    UNBANNED_NOTIFY = "You have been unbanned."
    UNCOOLDOWN_NOTIFY = "Your cooldown has been removed."

    BLOCK_USAGE = "Reply to a forwarded message with /block."
    BLOCKED_SENDER = "Sender blocked."
    UNBLOCK_EMPTY = "No blocked users to remove."
    UNBLOCKED_LAST = "Unblocked most recent sender."
    CANNOT_BLOCK_SELF = "Cannot block yourself."

    WHISPER_TEXT_REQUIRED = "Whisper replies must contain text."
    WHISPER_INSUFFICIENT_CREDITS = "Insufficient credits for whisper."
    WHISPER_CANNOT_SELF = "Cannot whisper yourself."
    WHISPER_UNAVAILABLE = "Recipient unavailable."
    WHISPER_USAGE = "Usage: /w @user <msg> OR reply /w <msg>"
    WHISPERMOD_USAGE = "Usage: /wmods <msg>"
    WHISPERMOD_SENT = "Modwhisper sent."

    DELETE_FOR_USERS = "Message deleted for users. Moderation action created."
    DELETE_WHISPER_FOR_USERS = "Whisper deleted for users. Moderation action created."
    DELETEVOTE_USAGE = "Reply to a message with /deletevote."
    DELETEVOTE_ALREADY = "You already voted to remove this message."
    MESSAGE_REMOVED = "Message removed."
    MESSAGE_REMOVED_BY_VOTE = "Message removed by vote."

    CONFIRMATION_CANCELLED = "Cancelled."
    TRIPCODE_SET_FIRST = "Use /settripcode name#secret first."
    UNSEND_USAGE = "Reply to one of your own sent messages with /unsend."
    UNSEND_OWN_ONLY = "You can only unsend your own message."
    UNSEND_NOT_SENT = "That message was not sent."
    UNSEND_NOT_DELIVERED = "That message has not been delivered yet."
    EDIT_OWN_ONLY = "You can only edit your own messages."
    EDIT_SAME_TYPE_ONLY = "Only the text or caption of the original message type can be edited."
    EDIT_REJECTED = "Edit was not applied because the new content did not pass checks."

    CREDIT_RECIPIENT_ATTACHED = "This transfer is attached to the replied message."
    CREDIT_TARGET_SELF = "Cannot transfer to yourself."
    CREDIT_NONZERO = "Amount must be non-zero."
    CREDIT_NEGATIVE_ADMIN_ONLY = "Only admins can send negative transfers."
    CREDIT_TRANSFER_FAILED = "Transfer failed (insufficient credits or invalid user)."
    CREDIT_INVALID_TARGET = "Target must be @username or numeric id."
    CREDIT_USAGE_REPLY = "Usage: reply /credit <amount>"
    CREDIT_USAGE_TARGET = "Usage: /credit @user <amount>"
    CREDIT_USAGE = "Usage: /credit @user <amount> OR reply /credit <amount>"

    FIGHTS_DISABLED = "Fights are disabled."
    FIGHT_UNAVAILABLE = "This user is not available for fights."
    FIGHT_SELF = "Cannot fight yourself."
    FIGHT_INVALID_STAKE = "Invalid stake."
    FIGHT_INITIATE_FAILED = "Cannot initiate fight with this user right now."
    FIGHT_USAGE = "Usage: /fight @user [amount] OR reply /fight [amount]"
    FIGHT_UNAVAILABLE_CALLBACK = "Fight no longer available."
    FIGHT_EXPIRED = "Fight expired."
    FIGHT_REQUEST_EXPIRED = "Fight request expired."
    FIGHT_NOT_YOURS = "Not your fight request."
    FIGHT_ACCEPT_FAILED = "Fight cannot proceed."
    FIGHT_ACCEPT_FAILED_NOTIFY = "Fight could not proceed at acceptance time."

    DUPLICATE_MEDIA = "This media has been sent already, please send something new"
    TOMBSTONE = "<i>This message was removed</i>"
    PENDING_MOD_ACTION = "This message is pending moderation action"
    CONFIRMED_REMOVAL = "Message was confirmed removal"
    IN_MODERATION_ACTION = "Message is in moderation action"

    @staticmethod
    def removed_pending(reason: str) -> str:
        return f"This message was removed and is pending moderation action. Reason: {reason}"

    REACT_USAGE = "Use Telegram reactions on a message: 👍 or ❤️ for upvote, 👎 for downvote."
    ABOUT_UPDATED = "About updated."
    SIGN_TOGGLE_DISABLED = "Persistent sign toggles are disabled."
    TRIPCODE_TOGGLE_DISABLED = "Persistent tripcode toggles are disabled."
    POTENTIALLY_UNWANTED_FILTER_LABEL = "Potentially unwanted filtering"
    VOTE_BUTTON_LABEL = "Vote button"
    DUPLICATE_FILTER_LABEL = "Duplicate media filtering"
    MOD_EXEMPT_SETTING = "Note: moderators and admins are exempt from this setting so moderation visibility is preserved."
    CREDIT_TARGET_NOT_FOUND = "Target user not found."
    CREDIT_TRANSFER_INVALID_USER = "Transfer failed (invalid user)."
    GAMBLE_USAGE = "Usage: /gamble <amount>"
    AMOUNT_NUMERIC = "Amount must be numeric."
    AMOUNT_MIN = "Amount must be at least 0.01."
    GAMBLE_TOO_MUCH = "Cannot gamble more than current credits."
    GAMBLE_MAX = "Amount exceeds configured max."
    TRIPCODESET_USAGE = "Usage: /settripcode name#secret"
    TRIPCODE_SEND_USAGE = "Usage: /t <message>"
    TRIPCODE_SENT = "Tripcoded message sent."
    SIGN_SEND_USAGE = "Usage: /s <message>"
    SIGNED_SENT = "Signed message sent."
    SENDINVITE_USAGE = "Usage: /sendinvite <telegram invite link> <reason/group description>"
    SENDINVITE_SENT = "Invite sent."
    SAUCE_USAGE = "Reply to an image/media message with /sauce."
    SAUCE_DISABLED = "SauceNAO search is not configured."
    SAUCE_NO_MEDIA = "No searchable media found on the replied message."
    SAUCE_BLURRED = "/sauce cannot be used on blurred messages."
    SAUCE_LIMITED = "SauceNAO usage limit reached. Try again later."
    SAUCE_NO_RESULTS = "No sauce found."
    SAUCE_LOOKUP_FAILED = "Sauce lookup failed."
    VOTE_OWN = "Cannot vote on your own message."
    VOTE_ALREADY = "You already voted on this message."
    VOTE_NO_CREDITS = "No credits left to vote."
    UPVOTE_ALREADY = "You already upvoted this message."
    DOWNVOTE_NO_CREDITS = "No credits left to downvote."
    DOWNVOTE_ALREADY = "You already downvoted this message."
    DELETEVOTE_OWN = "Cannot vote to remove your own message."
    DELETEVOTE_ALREADY_SHORT = "Already voted to remove this message."
    PUNISHMENT_CONFIRMED = "Punishment already confirmed."
    REMOVED_FOR_MODS_ALREADY = "Message already removed for mods."
    REMOVAL_REVERTED_ALREADY = "Removal already reverted."
    CONFIRMED_PUNISHMENT_APPLIED = "Confirmed. Punishment applied."
    REMOVED_FOR_MODS_SHORT = "Already removed for mods."
    REVERTED_ALREADY = "Already reverted."
    NO_VOTERS = "No voters to punish."
    REVERSAL_APPLIED = "Reversal punishment applied."
    MESSAGE_DELETED = "Message deleted."
    DELETION_CANCELLED = "Deletion cancelled."

    @staticmethod
    def enabled(label: str, enabled: bool) -> str:
        return f"{label} {'enabled' if enabled else 'disabled'}."

    @staticmethod
    def cooldown_remaining(remaining: str) -> str:
        return f"You are currently cooled down. Remaining: {remaining}."

    @staticmethod
    def initial_cooldown(minutes: int) -> str:
        return (
            f"You have to wait {minutes} minutes until you can chat here, however, "
            "you can use /invite to generate an invite link. Once someone joins with your link, "
            "your cooldown will be removed and you can instantly type. Until then, you can use this time to customize your experience and explore the bot's features with /help and watch the chat to see how others are using it!"
        )

    @staticmethod
    def whisper_unlock_required(amount: float) -> str:
        return f"Whisper unlock requires {amount:.2f} credits."

    @staticmethod
    def whisper_sent(cost: float, balance: float) -> str:
        return f"Whisper sent. Cost: {cost:.2f}. Balance: {balance:.2f}"

    @staticmethod
    def reload_failed(error: object) -> str:
        return f"Reload failed: {error}"

    @staticmethod
    def moderator_toggled(enabled: bool, target_id: int) -> str:
        return f"Moderator {'enabled' if enabled else 'disabled'} for {target_id}."

    @staticmethod
    def moderator_status_changed(enabled: bool) -> str:
        return f"You have been {'promoted to' if enabled else 'removed from'} moderator."

    @staticmethod
    def banned_target(target_id: int) -> str:
        return f"Banned: {target_id}"

    @staticmethod
    def unbanned_target(target_id: int) -> str:
        return f"Unbanned: {target_id}"

    @staticmethod
    def warning(text: str, suffix: str) -> str:
        return f"<i><b>Warning:</b></i> {text} <b><i>{suffix}</i></b>"

    @staticmethod
    def cooldown_received(seconds: int, reason: str) -> str:
        return f"You have been cooled down for {seconds}s. Reason: {reason}"

    @staticmethod
    def cooldown_set(target_id: int, seconds: int) -> str:
        return f"Cooldown set for {target_id} ({seconds}s)."

    @staticmethod
    def cooldown_removed(target_id: int) -> str:
        return f"Cooldown removed for {target_id}."

    @staticmethod
    def purged_banned(count: int) -> str:
        return f"Purged {count} messages from banned users."

    @staticmethod
    def whisper_removed_pending(reason: str) -> str:
        return f"This whisper was removed and is pending moderation action. Reason: {reason}"

    @staticmethod
    def tripcode_set(name_html: str, code: str) -> str:
        return f"Tripcode set: <b>{name_html}</b> !{code}"

    @staticmethod
    def invite_link(link: str) -> str:
        return f"Invite link:\n{link}"

    @staticmethod
    def invite_used(credits: float, balance: float) -> str:
        return f"Your invite was used. Earned {credits:.2f} credits. Balance: {balance:.2f}"

    @staticmethod
    def invite_used_cooldown_removed(credits: float, balance: float) -> str:
        return f"Your invite was used. Earned {credits:.2f} credits. Balance: {balance:.2f}. Your cooldown was removed."

    @staticmethod
    def unsent(cost: float, balance: float) -> str:
        return f"Message unsent. Cost: {cost:.2f}. Balance: {balance:.2f}"

    @staticmethod
    def unsend_insufficient(cost: float, balance: float) -> str:
        return f"/unsend costs {cost:.2f} credits. Current balance: {balance:.2f}."

    @staticmethod
    def edit_insufficient(cost: float, balance: float) -> str:
        return f"Editing costs {cost:.2f} credits. Current balance: {balance:.2f}."

    @staticmethod
    def edit_applied(cost: float, balance: float, edited_count: int) -> str:
        return (
            f"Edit applied for everyone else. Updated copies: {edited_count}. "
            f"Cost: {cost:.2f}. Balance: {balance:.2f}"
        )

    @staticmethod
    def sauce_credit_required(required: float, current: float) -> str:
        return f"/sauce requires at least {required:.2f} credits. Current balance: {current:.2f}."

    @staticmethod
    def sauce_result(title: str, similarity: float, author: str | None, urls: list[str], cached: bool, short_remaining: int | None, long_remaining: int | None) -> str:
        lines = [
            "Sauce result" + (" (cached)" if cached else ""),
            f"Title: {title or 'unknown'}",
            f"Similarity: {similarity:.2f}%",
        ]
        if author:
            lines.append(f"Author: {author}")
        if urls:
            lines.append("Links:")
            lines.extend(urls[:3])
        if short_remaining is not None or long_remaining is not None:
            lines.append("")
            lines.append(
                f"Remaining: short {short_remaining if short_remaining is not None else '?'} / daily {long_remaining if long_remaining is not None else '?'}")
        return "\n".join(lines)

    @staticmethod
    def fight_request_sent(fee: float, balance: float) -> str:
        return f"Fight request sent. Fee paid: {fee:.2f}. Balance: {balance:.2f}"

    @staticmethod
    def admin_credit_adjusted(amount: float, balance: float) -> str:
        return f"Adjusted target by {amount:.2f} credits. Target balance: {balance:.2f}"

    @staticmethod
    def credits_transferred(amount: float, balance: float) -> str:
        return f"Transferred {amount:.2f} credits. Balance: {balance:.2f}"

    @staticmethod
    def gamble_won(amount: float, balance: float) -> str:
        return f"You won {amount:.2f}. Balance: {balance:.2f}"

    @staticmethod
    def gamble_lost(amount: float, balance: float) -> str:
        return f"You lost {amount:.2f}. Balance: {balance:.2f}"

    @staticmethod
    def remove_vote_counted(count: int, threshold: int) -> str:
        return f"Remove vote counted ({count}/{threshold})."

    DELETEVOTE_DELETED_NOTIFY = "The message you voted to remove was deleted."

    @staticmethod
    def fight_request(rel: str) -> str:
        return f"Someone wants to fight you for an unknown stake.\nRelative matchup: {rel}"

    @staticmethod
    def upvote_cast(cost: float, remaining: float) -> str:
        return f"Upvote cast. Cost: {cost:.2f}. Remaining credits: {remaining:.2f}"

    @staticmethod
    def downvote_cast(cost: float, remaining: float, next_cost: float) -> str:
        return f"Downvote cast. Cost: {cost:.2f}. Remaining credits: {remaining:.2f}\nNext downvote cost: {next_cost:.2f}\nCost drop timer: 60s"

    @staticmethod
    def blurred_notice(loss_rate_percent: float) -> str:
        return (
            f"Uh oh 😭 The message was blurred due to your loss rate ({loss_rate_percent:.2f}%), "
            "earn credits to reduce your loss rate, see /help, /info and /creditstats for more info."
        )

    @staticmethod
    def removal_punishment(tax: float, balance: float, seconds: int) -> str:
        return f"Punishment applied.\nReason: removed message confirmed by moderators.\nCredits deducted: {tax:.2f}\nBalance: {balance:.2f}\nCooldown: {seconds}s"

    REVERT_SUCCESS = "Mods did not confirm anything wrong with your message and took action against the voters"

    @staticmethod
    def removed_for_mods(count: int) -> str:
        return f"Removed for mods. Copies updated: {count}."

    @staticmethod
    def reversal_punishment(tax: float, balance: float) -> str:
        return f"Punishment applied.\nReason: moderators reverted your remove vote.\nCredits deducted: {tax:.2f}\nBalance: {balance:.2f}"

    @staticmethod
    def fight_result(won: bool, delta: float, balance: float, matchup: str) -> str:
        return f"{'Won' if won else 'Lost'} fight. Delta: {delta:+.2f}. Balance: {balance:.2f}. {matchup}."

    FIGHT_DECLINED = "Fight request declined."
