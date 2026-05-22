from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


DescriptionProvider = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class HelpEntry:
    section: str
    command: str
    description: DescriptionProvider


def register_command(app: Any, command: str, handler: Any, section: str, description: DescriptionProvider) -> None:
    app.add_handler(handler)
    entries = app.bot_data.setdefault("help_entries", [])
    entries.append(HelpEntry(section=section, command=command, description=description))
