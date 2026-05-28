from __future__ import annotations

import hashlib


def compute_media_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()
