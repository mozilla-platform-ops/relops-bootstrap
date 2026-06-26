import time

from app.replay_cache import RateLimiter


def test_rate_limiter_first_burst_succeeds():
    rl = RateLimiter(requests_per_minute=60, burst=5)
    for _ in range(5):
        assert rl.consume(key="abc") is True


def test_rate_limiter_blocks_after_burst():
    rl = RateLimiter(requests_per_minute=60, burst=3)
    for _ in range(3):
        assert rl.consume(key="abc") is True
    # Burst exhausted; next call should fail until refill.
    assert rl.consume(key="abc") is False


def test_rate_limiter_independent_keys():
    rl = RateLimiter(requests_per_minute=60, burst=3)
    for _ in range(3):
        rl.consume(key="abc")
    # Different key has its own bucket.
    assert rl.consume(key="def") is True


def test_rate_limiter_refills_over_time():
    rl = RateLimiter(requests_per_minute=600, burst=1)  # 10 tokens/sec
    rl.consume(key="abc")
    assert rl.consume(key="abc") is False
    time.sleep(0.15)  # > 0.1s = 1 token
    assert rl.consume(key="abc") is True
