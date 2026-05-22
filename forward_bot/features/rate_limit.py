from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> tuple[bool, int]:
        now = time.time()
        q = self._hits[user_id]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) >= self.limit:
            retry = max(1, int(self.window_seconds - (now - q[0])))
            return False, retry
        q.append(now)
        return True, 0
