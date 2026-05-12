"""
rate_limit.py — Tiny in-memory rate limiter (Phase 12).

Per-key token bucket. Used to cap expensive endpoints (Q&A, AI re-analysis).
Resets each minute. Single-process only — for multi-worker we'd swap to Redis.
"""

import time
from collections import defaultdict
from threading import Lock

_buckets: dict[str, list[float]] = defaultdict(list)
_lock = Lock()


def take(key: str, limit_per_minute: int) -> bool:
    """Return True if a token was available, False if the limit is exhausted."""
    if limit_per_minute <= 0:
        return True
    now = time.monotonic()
    with _lock:
        bucket = _buckets[key]
        # Drop entries older than 60 seconds.
        cutoff = now - 60.0
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit_per_minute:
            return False
        bucket.append(now)
        return True
