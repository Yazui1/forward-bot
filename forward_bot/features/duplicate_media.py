from __future__ import annotations

import hashlib


def media_digest(preview_bytes: bytes | None) -> str | None:
    if not preview_bytes:
        return None
    return hashlib.blake2b(preview_bytes, digest_size=16).hexdigest()
