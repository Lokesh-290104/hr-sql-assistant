"""In-memory rate limits for the endpoint that spends LLM quota.

A public demo must not let one visitor (or a script) burn the whole LLM quota, so questions
are limited per client per minute and in total per day. When a limit is hit the request is
refused before any model is called (fail closed). State is per process, which is enough for
the single-instance deployment; several instances would need a shared store such as Redis.
"""

import threading
import time
from collections import deque
from collections.abc import Callable

WINDOW_SECONDS = 60.0
SECONDS_PER_DAY = 86_400
# Forget idle clients once this many are tracked, so memory stays bounded.
MAX_TRACKED_CLIENTS = 10_000


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int, clock: Callable[[], float] = time.time):
        """A limit of 0 disables that limit."""
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: dict[str, deque[float]] = {}
        self._day = -1
        self._day_count = 0

    def check(self, client: str) -> str | None:
        """Record one request from `client`. Returns why it is refused, or None if allowed."""
        now = self._clock()
        with self._lock:
            day = int(now // SECONDS_PER_DAY)
            if day != self._day:
                self._day, self._day_count = day, 0
            if self.per_day and self._day_count >= self.per_day:
                return "The demo has reached its question limit for today. Please try again tomorrow."

            recent = self._recent.setdefault(client, deque())
            while recent and recent[0] <= now - WINDOW_SECONDS:
                recent.popleft()
            if self.per_minute and len(recent) >= self.per_minute:
                return f"Too many questions: at most {self.per_minute} per minute. Please wait a moment."

            recent.append(now)
            self._day_count += 1
            if len(self._recent) > MAX_TRACKED_CLIENTS:
                self._forget_idle(now)
            return None

    def _forget_idle(self, now: float) -> None:
        for client in [c for c, times in self._recent.items() if not times or times[-1] <= now - WINDOW_SECONDS]:
            del self._recent[client]
