"""Per-IP rate limiting for POST /verify (PRD Slice 13, D56).

Exposure hygiene for an invited-attack surface, not auth (the D40
classification): a generous ceiling per client IP over a sliding 60-second
window, HTTP 429 above it — transport-level, like 404/405 (no Decision body is
owed to a request the limiter refused; D35). A limit of 0 disables the
limiter — an operator's explicit act, never a default.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict

DEFAULT_MAX_PER_MINUTE = 120
_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sliding-window counter per key, one lock (FastAPI serves sync endpoints
    from a threadpool). Idle keys are pruned on the way through, so memory is
    bounded by the number of clients active in any one window."""

    def __init__(
        self,
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._max > 0

    def allow(self, key: str) -> bool:
        """True if this request is within the caller's budget."""
        if not self.enabled:
            return True
        now = self._clock()
        horizon = now - _WINDOW_SECONDS
        with self._lock:
            for stale_key in [k for k, q in self._hits.items() if not q or q[-1] <= horizon]:
                if stale_key != key:
                    del self._hits[stale_key]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= horizon:
                hits.popleft()
            if len(hits) >= self._max:
                return False
            hits.append(now)
            return True


def build_rate_limiter() -> RateLimiter:
    """Wire from ``AGENTGATE_RATE_LIMIT_PER_MINUTE`` (default 120; 0 disables).
    A malformed value fails loudly at startup — a silently-dropped limit would
    be a gate quietly weaker than its written config (D28)."""
    raw = os.environ.get("AGENTGATE_RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return RateLimiter()
    value = int(raw)
    if value < 0:
        raise ValueError("AGENTGATE_RATE_LIMIT_PER_MINUTE must be >= 0 (0 disables).")
    return RateLimiter(max_per_minute=value)


def client_key(headers: dict, fallback: str) -> str:
    """The limiter key: the first X-Forwarded-For hop when present (the app sits
    behind a reverse proxy in every deployed posture), else the socket peer."""
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return fallback
