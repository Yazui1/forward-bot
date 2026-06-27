from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta

from forward_bot.utils import now_utc


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, int(limit))
        self.window = timedelta(seconds=max(1, int(window_seconds)))
        self.events: dict[int, deque] = defaultdict(deque)

    def update_config(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, int(limit))
        self.window = timedelta(seconds=max(1, int(window_seconds)))

    def check(self, user_id: int) -> tuple[bool, int]:
        q = self.events[user_id]
        now = now_utc()
        while q and q[0] + self.window < now:
            q.popleft()
        if len(q) >= self.limit:
            retry = int((q[0] + self.window - now).total_seconds()) + 1
            return False, max(1, retry)
        q.append(now)
        return True, 0

    def remaining_seconds(self, user_id: int) -> int:
        q = self.events[user_id]
        now = now_utc()
        while q and q[0] + self.window < now:
            q.popleft()
        if len(q) < self.limit:
            return 0
        return max(1, int((q[0] + self.window - now).total_seconds()) + 1)
