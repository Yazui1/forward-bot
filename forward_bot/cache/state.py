from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from typing import Any

from forward_bot.utils import now_utc


class TTLMap:
    def __init__(self, ttl_seconds: int, max_size: int = 10000):
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_size = max_size
        self._items: OrderedDict[Any, tuple[Any, object]] = OrderedDict()

    def set(self, key: Any, value: object) -> None:
        self.cleanup()
        self._items[key] = (now_utc(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)

    def get(self, key: Any) -> object | None:
        item = self._items.get(key)
        if not item:
            return None
        created, value = item
        if created + self.ttl < now_utc():  # type: ignore[operator]
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def cleanup(self) -> None:
        expired = [k for k, (created, _) in self._items.items() if created + self.ttl < now_utc()]  # type: ignore[operator]
        for key in expired:
            self._items.pop(key, None)
