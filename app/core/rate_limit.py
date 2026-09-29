"""A small in-process sliding-window rate limiter for the auth endpoints.

DOC section 31 asks for throttling on authentication routes. Phase 1 runs a
single Uvicorn worker, so an in-memory counter is enough and avoids adding
Redis to the stack. `README.md` documents how to move this to Redis when the
API is scaled horizontally.

Keyed by client IP *and* the submitted email, so one attacker cannot lock every
account out by hammering a single address.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(slots=True)
class RateLimit:
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")


class InMemoryRateLimiter:
    """Fixed-window counters keyed by an arbitrary string.

    The window resets wholesale rather than sliding, which is slightly more
    bursty but far cheaper to reason about -- and it is the right trade for a
    brute-force guard, not a billing quota.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, rule: RateLimit) -> tuple[bool, int, int]:
        """Record a hit for `key` and report whether it is allowed.

        Returns `(allowed, remaining, retry_after_seconds)`.
        """
        now = time.monotonic()
        cutoff = now - rule.window_seconds

        hits = self._hits[key]
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= rule.limit:
            retry_after = max(1, int(hits[0] + rule.window_seconds - now) + 1)
            return False, 0, retry_after

        hits.append(now)
        return True, rule.limit - len(hits), 0

    def reset(self, key: str | None = None) -> None:
        """Clear one key, or every key when called with no argument (tests)."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


# One shared instance; the lifespan does not need to know about it.
rate_limiter = InMemoryRateLimiter()
