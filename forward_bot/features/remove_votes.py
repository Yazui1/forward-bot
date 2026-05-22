from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from forward_bot.utils import as_utc


async def check_remove_vote_allowed(repo: Any, cfg: dict[str, Any], voter_id: int) -> tuple[bool, str | None]:
    cooldown = await repo.get_active_cooldown(voter_id)
    if cooldown is not None:
        try:
            until = as_utc(str(cooldown["until_at"]))
            wait = max(0, int((until - datetime.now(timezone.utc)).total_seconds()))
            return False, f"You are currently cooled down. Remaining: {wait}s."
        except ValueError:
            return False, "You are currently cooled down."

    voter = await repo.get_user(voter_id)
    if voter is None:
        return False, "User not found."
    top_fraction = float(cfg["vote_to_remove"].get("voter_min_top_credit_percentile", -1))
    if top_fraction > 1:
        top_fraction = top_fraction / 100.0
    if top_fraction >= 0:
        cutoff = await repo.credit_cutoff_for_top_fraction(top_fraction)
        if float(voter.credits) < cutoff:
            pct = int(top_fraction * 100)
            return False, f"Remove voting requires at least {cutoff:.2f} credits (top {pct}% cutoff)."

    user_vote_cd = int(cfg["vote_to_remove"]["user_vote_cooldown_seconds"])
    last_vote = await repo.user_last_remove_vote_at(voter_id)
    if last_vote:
        try:
            last_vote_at = as_utc(last_vote)
            wait = user_vote_cd - int((datetime.now(timezone.utc) - last_vote_at).total_seconds())
            if wait > 0:
                return False, f"Remove vote cooldown active ({wait}s)."
        except ValueError:
            pass

    user_remove_limit = int(cfg["vote_to_remove"]["user_remove_limit"])
    user_remove_window = int(cfg["vote_to_remove"]["user_remove_cooldown_seconds"])
    used = await repo.count_user_remove_votes_in_window(voter_id, user_remove_window)
    if used >= user_remove_limit:
        return False, "Your remove-vote limit is reached for now."

    global_limit = int(cfg["vote_to_remove"]["global_limit"])
    global_window = int(cfg["vote_to_remove"]["global_cooldown_seconds"])
    global_used = await repo.count_global_removals_in_window(global_window)
    if global_used >= global_limit:
        return False, "Global remove limit reached. Try later."

    return True, None
