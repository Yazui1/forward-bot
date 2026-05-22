from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class Config:
    def __init__(self, path: str = "config.yml") -> None:
        self.path = Path(path)
        self.data = self._load()
        self._validate()

    def _load(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        for key, value in os.environ.items():
            raw = raw.replace(f"${{{key}}}", value)
        return yaml.safe_load(raw)

    def _validate(self) -> None:
        for key in ("bot", "database", "rate_limits"):
            if key not in self.data:
                raise ValueError(f"Missing required config section: {key}")
        token = self.data["bot"].get("token", "")
        if not token or token.startswith("${"):
            raise ValueError("bot.token must be configured")

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(key)
            if node is None:
                return default
        return node

    def __getitem__(self, item: str) -> Any:
        return self.data[item]
