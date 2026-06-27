from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram.ext import Application, CommandHandler


@dataclass(slots=True)
class CommandSpec:
    name: str
    section: str
    description: str
    handler: object
    mod: bool = False
    admin: bool = False


class HelpRegistry:
    def __init__(self) -> None:
        self.commands: list[CommandSpec] = []

    def add(self, name: str, section: str, description: str, handler, *, mod: bool = False, admin: bool = False) -> None:
        self.commands.append(CommandSpec(name, section, description, handler, mod, admin))

    def register(self, app: Application) -> None:
        for spec in self.commands:
            app.add_handler(CommandHandler(spec.name, spec.handler))

    def help_text(self, *, include_mod: bool, include_admin: bool, config: Any | None = None) -> str:
        lines: list[str] = ["Commands:"]
        current = None
        for spec in self.commands:
            if spec.admin and not include_admin:
                continue
            if spec.mod and not (include_mod or include_admin):
                continue
            if spec.section != current:
                current = spec.section
                lines.append(f"\n{current}:")
            lines.append(f"/{spec.name} - {_description(spec, config)}")
        return "\n".join(lines)


def _description(spec: CommandSpec, config: Any | None) -> str:
    if config is None:
        return spec.description
    name = spec.name
    if name == "unsend":
        return f"Remove your own replied message for {float(config.get('credits.unsend_cost', 0) or 0):.2f} credits."
    if name == "w":
        return (
            "Whisper by reply/reference. "
            f"Cost {float(config.get('credits.whisper_cost', 0) or 0):.2f}, unlock {float(config.get('credits.whisper_unlock_credits', 0) or 0):.2f} credits."
        )
    if name == "gamble":
        return f"Gamble credits with 50% odds, max {float(config.get('gamble.max_amount', 0) or 0):.2f}."
    if name == "fight":
        return f"Challenge a user for credits, max stake {float(config.get('fights.max_stake_cap', 0) or 0):.2f}."
    if name == "deletevote":
        return f"Vote to remove a replied message; threshold {int(config.get('vote_to_remove.threshold', 0) or 0)}."
    if name == "sauce":
        limit = int(config.get("saucenao.per_user_daily_limit", -1) or -1)
        return f"Search source for replied media; daily limit {'unlimited' if limit < 0 else limit}."
    if name == "creditstats":
        return "Show credit leaderboard, caps, rewards, costs, tax, and loss-rate details."
    return spec.description
