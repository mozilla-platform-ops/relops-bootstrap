"""
Per-cert-serial token-bucket rate limiter.

In-memory and per-Cloud-Run-instance. For low-QPS use this is plenty; upgrade
to Memorystore for shared state across instances if QPS ever warrants it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """
    Token bucket per cert serial. Each bucket refills at requests_per_minute / 60 tokens/sec
    and tops out at burst.
    """

    def __init__(self, requests_per_minute: int, burst: int) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._refill_rate = requests_per_minute / 60.0  # tokens/sec
        self._burst = burst
        self._idle_gc_seconds = 600.0

    def _gc(self, now: float) -> None:
        if len(self._buckets) < 1024:
            return
        cutoff = now - self._idle_gc_seconds
        stale = [k for k, b in self._buckets.items() if b.last_refill < cutoff]
        for k in stale:
            del self._buckets[k]

    def consume(self, key: str, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            self._gc(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._burst), last_refill=now)
                self._buckets[key] = bucket

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._refill_rate)
            bucket.last_refill = now

            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False
