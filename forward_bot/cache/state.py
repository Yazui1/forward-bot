from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class CachedSenderMetadata:
    sender_id: int
    username: str | None
    temporal_id: str
    role: str
    credits: float
    cached_at: float


class SenderMetadataCache:
    def __init__(self, max_size: int, ttl_seconds: int) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._data: OrderedDict[int, CachedSenderMetadata] = OrderedDict()

    def set(self, key: int, value: CachedSenderMetadata) -> None:
        now = time.time()
        value.cached_at = now
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)
        self._evict_expired()

    def get(self, key: int) -> CachedSenderMetadata | None:
        self._evict_expired()
        value = self._data.get(key)
        if value is None:
            return None
        self._data.move_to_end(key)
        return value

    def _evict_expired(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        expired = [k for k, v in self._data.items() if v.cached_at < cutoff]
        for key in expired:
            self._data.pop(key, None)


class EphemeralState:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._confirmations: dict[int, tuple[dict[str, Any], float]] = {}
        self._retries: dict[str, tuple[dict[str, Any], float]] = {}

    def set_confirmation(self, message_id: int, data: dict[str, Any]) -> None:
        self._confirmations[message_id] = (data, time.time())

    def pop_confirmation(self, message_id: int) -> dict[str, Any] | None:
        item = self._confirmations.pop(message_id, None)
        if item is None:
            return None
        data, ts = item
        if time.time() - ts > self.ttl_seconds:
            return None
        return data

    def set_retry(self, token: str, data: dict[str, Any]) -> None:
        self._retries[token] = (data, time.time())

    def pop_retry(self, token: str) -> dict[str, Any] | None:
        item = self._retries.pop(token, None)
        if item is None:
            return None
        data, ts = item
        if time.time() - ts > self.ttl_seconds:
            return None
        return data
