import time

from app.replay_cache import JtiCache, RateLimiter


def test_jti_cache_first_sight_returns_false():
    cache = JtiCache(max_size=10)
    assert cache.seen_recently("abc", ttl_seconds=60) is False


def test_jti_cache_second_sight_returns_true():
    cache = JtiCache(max_size=10)
    cache.seen_recently("abc", ttl_seconds=60)
    assert cache.seen_recently("abc", ttl_seconds=60) is True


def test_jti_cache_evicts_after_ttl():
    cache = JtiCache(max_size=10)
    cache.seen_recently("abc", ttl_seconds=0.05)
    time.sleep(0.1)
    # After TTL, the next call evicts the expired entry and treats jti as new.
    assert cache.seen_recently("abc", ttl_seconds=0.05) is False


def test_jti_cache_lru_eviction():
    """
    Once max_size is exceeded, the least-recently-added jti is evicted.
    Note: looking up an evicted jti returns False AND re-adds it, which can
    cascade and evict other entries. The contract is "evicted entries appear
    as new"; we don't promise anything more granular.
    """
    cache = JtiCache(max_size=2)
    cache.seen_recently("a", 60)
    cache.seen_recently("b", 60)
    cache.seen_recently("c", 60)  # cache is now {b, c}
    # "a" was evicted, so it appears as new.
    assert cache.seen_recently("a", 60) is False


def test_rate_limiter_first_burst_succeeds():
    rl = RateLimiter(requests_per_minute=60, burst=5)
    for _ in range(5):
        assert rl.consume(key=1) is True


def test_rate_limiter_blocks_after_burst():
    rl = RateLimiter(requests_per_minute=60, burst=3)
    for _ in range(3):
        assert rl.consume(key=1) is True
    # Burst exhausted; next call should fail until refill.
    assert rl.consume(key=1) is False


def test_rate_limiter_independent_keys():
    rl = RateLimiter(requests_per_minute=60, burst=3)
    for _ in range(3):
        rl.consume(key=1)
    # Different key has its own bucket.
    assert rl.consume(key=2) is True


def test_rate_limiter_refills_over_time():
    rl = RateLimiter(requests_per_minute=600, burst=1)  # 10 tokens/sec
    rl.consume(key=1)
    assert rl.consume(key=1) is False
    time.sleep(0.15)  # > 0.1s = 1 token
    assert rl.consume(key=1) is True
