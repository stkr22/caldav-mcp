"""Sliding-window limiter for failed authentication attempts, keyed by client address."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

_MAX_KEYS = 10_000  # a spoofed-address flood must not grow memory without bound
_BACKOFF_BASE_SECONDS = 5
_BACKOFF_CAP_SECONDS = 300


class RateLimiter:
    """Thread-safe: after *max_failures* failures within *window_seconds* a key is blocked."""

    def __init__(self, max_failures: int, window_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        """Block a key after *max_failures* failures within *window_seconds*."""
        self._max = max_failures
        self._window = window_seconds
        self._clock = clock
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self._window]
        if recent:
            self._failures[key] = recent
            self._failures.move_to_end(key)
        else:
            self._failures.pop(key, None)
        return recent

    def record_failure(self, key: str) -> None:
        """Count one failed attempt."""
        now = self._clock()
        with self._lock:
            recent = self._recent(key, now)
            self._failures[key] = [*recent, now]
            self._failures.move_to_end(key)
            while len(self._failures) > _MAX_KEYS:
                self._failures.popitem(last=False)

    def retry_after(self, key: str) -> int:
        """Seconds to wait before the next attempt; 0 when not blocked."""
        now = self._clock()
        with self._lock:
            count = len(self._recent(key, now))
        if count < self._max:
            return 0
        return int(min(_BACKOFF_BASE_SECONDS * 2 ** min(count - self._max, 6), _BACKOFF_CAP_SECONDS))

    def reset(self, key: str) -> None:
        """Forget a key after a successful attempt."""
        with self._lock:
            self._failures.pop(key, None)
