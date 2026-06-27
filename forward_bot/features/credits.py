from __future__ import annotations

from datetime import timedelta

from forward_bot.config import Config
from forward_bot.db.repository import Repository, User
from forward_bot.features.interpolation import interpolate
from forward_bot.utils import now_utc, parse_dt, round_credits


def daily_caps(config: Config) -> dict[str, float]:
    raw = config.section("credits.daily_earning_limits")
    return {str(k): float(v) for k, v in raw.items()}


def tax_rate(config: Config, credits: float) -> float:
    return interpolate(config.get("credits.tax_ramp", []), "credits", "daily_tax_percent", credits, 0.0)


def loss_rate(config: Config, credits: float) -> float:
    return interpolate(config.get("loss_rate.schedule", []), "credits", "loss_rate", credits, 0.0)


def apply_credit(
    repo: Repository,
    config: Config,
    user_id: int,
    amount: float,
    reason: str,
    *,
    cap_positive: bool = True,
) -> tuple[float, User | None]:
    caps = daily_caps(config) if cap_positive else None
    applied, user = repo.apply_credit_change(user_id, round_credits(amount), reason, daily_caps=caps)
    maybe_apply_negative_cooldown(repo, config, user)
    return applied, user


def maybe_apply_negative_cooldown(repo: Repository, config: Config, user: User | None) -> None:
    if not user or user.credits >= 0 or user.is_mod_or_admin:
        return
    seconds = int(config.get("credits.negative_credit_cooldown_seconds", 0) or 0)
    if seconds > 0:
        repo.set_cooldown(user.telegram_id, seconds, "negative credits", None, stack=False)


def downvote_cost(config: Config, streak: int, last_downvote_at: str | None) -> tuple[float, int, int]:
    schedule = config.get("credits.downvote_cost_schedule", []) or []
    decayed = _decayed_streak(streak, last_downvote_at)
    next_streak = decayed + 1
    if not schedule:
        return float(config.get("credits.downvote_start_cost", 1.0)), next_streak, 0
    points = sorted((int(item.get("minute", 0)), float(item.get("cost", 0))) for item in schedule)
    idx = min(next_streak - 1, len(points) - 1)
    next_drop = 0
    last = parse_dt(last_downvote_at)
    if last and idx < len(points):
        next_drop = max(0, int(((last + timedelta(minutes=points[idx][0])) - now_utc()).total_seconds()))
    return points[idx][1], next_streak, next_drop


def downvote_drop_seconds(config: Config, streak: int, last_downvote_at: str | None) -> int:
    if streak <= 0:
        return 0
    schedule = config.get("credits.downvote_cost_schedule", []) or []
    if not schedule:
        return 0
    points = sorted((int(item.get("minute", 0)), float(item.get("cost", 0))) for item in schedule)
    idx = min(streak - 1, len(points) - 1)
    last = parse_dt(last_downvote_at)
    if not last:
        return 0
    return max(0, int(((last + timedelta(minutes=points[idx][0])) - now_utc()).total_seconds()))


def _decayed_streak(streak: int, last_downvote_at: str | None) -> int:
    last = parse_dt(last_downvote_at)
    if not last:
        return 0
    minutes = max(0, int((now_utc() - last).total_seconds() // 60))
    return max(0, streak - minutes)
