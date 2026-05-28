from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any

from telegram.error import Forbidden

from forward_bot.features.credits import apply_negative_credit_cooldown, interpolate_tax_rate


async def daily_tax_worker(repo: Any, cfg: dict[str, Any]) -> None:
    if not bool(cfg["credits"].get("daily_tax_enabled", True)):
        return
    interval = int(cfg["credits"].get("daily_tax_check_interval_seconds", 3600))
    while True:
        users = await repo.list_users()
        today = datetime.now(timezone.utc).date().isoformat()
        for user in users:
            if user.is_banned:
                continue
            rate = interpolate_tax_rate(cfg["credits"]["tax_ramp"], user.credits)
            amount = max(0.0, user.credits * rate)
            applied = await repo.apply_daily_tax_once(user.telegram_id, today, amount)
            if applied:
                refreshed = await repo.get_user(user.telegram_id)
                if refreshed is not None:
                    await apply_negative_credit_cooldown(repo, cfg, user.telegram_id, refreshed.credits, 0)
        await asyncio.sleep(max(60, interval))


async def tips_worker(bot: Any, repo: Any, cfg: dict[str, Any]) -> None:
    tips_cfg = cfg.get("tips", {})
    if not bool(tips_cfg.get("enabled", True)):
        return
    messages = list(tips_cfg.get("messages", []))
    if not messages:
        return
    interval = int(float(tips_cfg.get("interval_hours", 24)) * 3600)
    while True:
        users = await repo.list_users()
        for user in users:
            if not user.has_started or user.is_banned:
                continue
            try:
                await bot.send_message(chat_id=user.telegram_id, text=random.choice(messages))
            except Forbidden:
                await repo.mark_left(user.telegram_id)
            except Exception:
                pass
        await asyncio.sleep(max(300, interval))
