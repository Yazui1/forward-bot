from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class Config:
    def __init__(self, data: dict[str, Any], path: str | Path = "config.yml"):
        self.data = data
        self.path = Path(path)

    @classmethod
    def load(cls, path: str | Path = "config.yml") -> "Config":
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw) or {}
        return cls(_substitute_env(loaded), p)

    def reload(self) -> "Config":
        return Config.load(self.path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def copy(self) -> "Config":
        return Config(deepcopy(self.data), self.path)


def _substitute_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            fallback = match.group(2) or ""
            return os.getenv(name, fallback)

        return _ENV_RE.sub(repl, value)
    return value
