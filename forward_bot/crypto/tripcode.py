from __future__ import annotations

import hashlib


def hash_tripcode(secret: str, global_salt: str) -> str:
    digest = hashlib.sha256(f"{global_salt}:{secret}".encode("utf-8")).hexdigest()
    return digest[:6]
