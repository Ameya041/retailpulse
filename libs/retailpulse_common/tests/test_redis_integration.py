"""Integration tests against a real Redis instance.

The unit tests use in-memory doubles, which prove the *logic*. They cannot
prove the thing that actually matters about the rate limiter: that the Lua
script is atomic, so concurrent requests cannot both pass a check that only one
should. That needs a real Redis and real threads.

Skipped when Redis is unreachable.

    docker compose up -d redis
    pytest -m redis
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from retailpulse_common.cache import RedisCache, cached, invalidate_prefix
from retailpulse_common.rate_limit import RedisRateLimiter

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/15")


def _redis_available() -> bool:
    try:
        return RedisCache(REDIS_URL, service_name="itest").ping()
    except Exception:
        return False


pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(
        not _redis_available(), reason="Redis is not reachable; run `docker compose up -d redis`"
    ),
]


@pytest.fixture()
def cache() -> RedisCache:
    return RedisCache(REDIS_URL, service_name="itest")


@pytest.fixture()
def limiter() -> RedisRateLimiter:
    return RedisRateLimiter(REDIS_URL)


# ---------------------------------------------------------------------------
# Cache against real Redis
# ---------------------------------------------------------------------------
def test_value_round_trips_through_redis(cache):
    key = f"itest:product:{uuid.uuid4()}"
    calls = []

    def loader():
        calls.append(1)
        return {"name": "Widget", "price": "199.99"}

    first = cached(cache, key, loader, ttl_seconds=30)
    second = cached(cache, key, loader, ttl_seconds=30)

    assert first == second == {"name": "Widget", "price": "199.99"}
    assert len(calls) == 1, "second read should have been served from Redis"
    cache.delete(key)


def test_ttl_is_actually_set_on_the_key(cache):
    key = f"itest:ttl:{uuid.uuid4()}"
    cache.set(key, "value", 45)

    assert 0 < cache._redis.ttl(key) <= 45
    cache.delete(key)


def test_prefix_invalidation_uses_scan_and_clears_matching_keys(cache):
    prefix = f"itest:cat:{uuid.uuid4()}:"
    for page in range(5):
        cache.set(f"{prefix}{page}", "[]", 60)
    other = f"itest:other:{uuid.uuid4()}"
    cache.set(other, "[]", 60)

    removed = invalidate_prefix(cache, prefix)

    assert removed == 5
    assert cache.get(other) is not None
    cache.delete(other)


def test_delete_returns_the_number_actually_removed(cache):
    key = f"itest:{uuid.uuid4()}"
    cache.set(key, "v", 30)

    assert cache.delete(key) == 1
    assert cache.delete(key) == 0


# ---------------------------------------------------------------------------
# Rate limiting under real concurrency
# ---------------------------------------------------------------------------
def test_limit_is_enforced_exactly(limiter):
    identity = f"itest:{uuid.uuid4()}"

    allowed = [limiter.check(identity, limit=10, window_seconds=60).allowed for _ in range(15)]

    assert sum(allowed) == 10
    assert allowed[:10] == [True] * 10
    assert allowed[10:] == [False] * 5
    limiter.reset(identity)


def test_concurrent_requests_cannot_slip_past_the_limit(limiter):
    """The reason the check runs as a Lua script.

    Without atomicity, 50 threads all read the same count and all decide they
    are under the limit -- the same lost-update race as the inventory oversell.
    """
    identity = f"itest:{uuid.uuid4()}"

    with ThreadPoolExecutor(max_workers=25) as pool:
        outcomes = list(
            pool.map(
                lambda _: limiter.check(identity, limit=20, window_seconds=60).allowed,
                range(50),
            )
        )

    assert sum(outcomes) == 20, f"expected exactly 20 to pass, got {sum(outcomes)}"
    limiter.reset(identity)


def test_two_identities_do_not_share_a_budget(limiter):
    a, b = f"itest:{uuid.uuid4()}", f"itest:{uuid.uuid4()}"

    for _ in range(5):
        limiter.check(a, limit=5, window_seconds=60)

    assert limiter.check(a, limit=5, window_seconds=60).allowed is False
    assert limiter.check(b, limit=5, window_seconds=60).allowed is True
    limiter.reset(a)
    limiter.reset(b)


def test_retry_after_is_bounded_by_the_window(limiter):
    identity = f"itest:{uuid.uuid4()}"
    limiter.check(identity, limit=1, window_seconds=60)

    result = limiter.check(identity, limit=1, window_seconds=60)

    assert result.allowed is False
    assert 0 < result.retry_after_seconds <= 60
    limiter.reset(identity)


def test_the_window_key_expires_so_idle_callers_do_not_leak(limiter):
    """Without PEXPIRE, every identity that ever called would live forever."""
    identity = f"itest:{uuid.uuid4()}"
    limiter.check(identity, limit=5, window_seconds=30)

    ttl_ms = limiter._redis.pttl(f"ratelimit:{identity}")

    assert 0 < ttl_ms <= 30_000
    limiter.reset(identity)


def test_reset_clears_the_window(limiter):
    identity = f"itest:{uuid.uuid4()}"
    for _ in range(3):
        limiter.check(identity, limit=3, window_seconds=60)
    assert limiter.check(identity, limit=3, window_seconds=60).allowed is False

    limiter.reset(identity)

    assert limiter.check(identity, limit=3, window_seconds=60).allowed is True
    limiter.reset(identity)
