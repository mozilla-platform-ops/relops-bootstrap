"""
JTI replay cache + per-cert-serial token-bucket rate limiter.

Both are in-memory and per-Cloud-Run-instance. With multiple instances, replay is
still possible across instances within the JWT lifetime. For low-QPS use (EACS is
human-paced), the practical exposure is low; upgrade to Memorystore/Firestore for
shared state if QPS warrants.

Thread-safe via threading.Lock — FastAPI runs sync handlers on a thread pool
and async handlers in the event loop; both can hit these caches concurrently.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


class JtiCache:
    """LRU set with per-entry TTL. seen_recently(jti, ttl) is True if jti was added in the last ttl seconds."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._max_size = max_size
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _evict_expired(self, now: float, ttl: float) -> None:
        cutoff = now - ttl
        while self._entries:
            jti, ts = next(iter(self._entries.items()))
            if ts < cutoff:
                self._entries.popitem(last=False)
            else:
                break

    def seen_recently(self, jti: str, ttl_seconds: float) -> bool:
        """Returns True if jti has been seen within ttl_seconds. Adds jti to cache on first sight."""
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now, ttl_seconds)

            if jti in self._entries:
                return True

            self._entries[jti] = now
            self._entries.move_to_end(jti)

            # LRU evict if over capacity
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)

            return False


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
        self._buckets: dict[int, _Bucket] = {}
        self._refill_rate = requests_per_minute / 60.0  # tokens/sec
        self._burst = burst
        # Garbage-collect buckets idle longer than this; safe default = 10 min.
        self._idle_gc_seconds = 600.0

    def _gc(self, now: float) -> None:
        if len(self._buckets) < 1024:
            return
        cutoff = now - self._idle_gc_seconds
        stale = [k for k, b in self._buckets.items() if b.last_refill < cutoff]
        for k in stale:
            del self._buckets[k]

    def consume(self, key: int, tokens: float = 1.0) -> bool:
        """Try to take `tokens` from key's bucket. Returns True on success, False if rate-limited."""
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
