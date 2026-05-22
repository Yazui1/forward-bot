from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from forward_bot.features.interpolation import linear_interpolate


@dataclass(frozen=True)
class InflationStats:
    daily_percent: float
    weekly_percent: float


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def round_credit(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def interpolate_tax_rate(tax_ramp: list[dict[str, Any]], credits: float) -> float:
    points = [(float(x["credits"]), float(x["daily_tax_percent"])) for x in tax_ramp]
    return linear_interpolate(points, credits)


def interpolate_loss_rate(loss_schedule: list[dict[str, Any]], credits: float) -> float:
    points = [(float(x["credits"]), float(x["loss_rate"])) for x in loss_schedule]
    return linear_interpolate(points, credits)


def interpolate_downvote_cost(schedule: list[dict[str, Any]], minute_value: float, start_cost: float) -> float:
    points = [(float(x["minute"]), float(x["cost"])) for x in schedule]
    if not points:
        return round_credit(start_cost)
    return round_credit(linear_interpolate(points, max(1.0, minute_value)))


async def apply_negative_credit_cooldown(repo: Any, cfg: dict[str, Any], user_id: int, balance: float, applied_by: int = 0) -> None:
    if balance >= 0:
        return
    seconds = int(cfg["credits"].get("negative_credit_cooldown_seconds", 3600))
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    await repo.set_cooldown(user_id, until.isoformat(), "negative-credit", applied_by)


async def adjust_credits_with_daily_limit(repo: Any, cfg: dict[str, Any], user_id: int, amount: float, reason: str) -> tuple[float, float]:
    amount = round_credit(amount)
    if amount <= 0:
        balance = await repo.adjust_credits(user_id, amount, reason)
        return balance, amount

    limits = cfg.get("credits", {}).get("daily_earning_limits", {})
    limit = float(limits.get(reason, -1))
    if limit >= 0:
        earned_today = await repo.positive_credits_today(user_id, reason)
        amount = round_credit(max(0.0, min(amount, limit - earned_today)))

    if amount <= 0:
        user = await repo.get_user(user_id)
        return (user.credits if user else 0.0), 0.0

    balance = await repo.adjust_credits(user_id, amount, reason)
    return balance, amount
