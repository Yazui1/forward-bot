from __future__ import annotations

import hashlib
import re


_NAME_RE = re.compile(r"^[^\s#!]{1,32}$")


def make_tripcode(name: str, secret: str, salt: str) -> tuple[str, str]:
    name = name.strip()
    secret = secret.strip()
    if not _NAME_RE.fullmatch(name) or not secret:
        raise ValueError("Use /settripcode name#secret with a short name and secret.")
    digest = hashlib.blake2s(f"{salt}:{name}:{secret}".encode(), digest_size=4).hexdigest()
    return name, digest


def display_tripcode(name: str | None, code: str | None) -> str | None:
    if not name or not code:
        return None
    return f"{name} !{code}"
