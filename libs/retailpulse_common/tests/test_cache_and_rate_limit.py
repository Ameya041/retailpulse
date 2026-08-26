"""Tests for the cache-aside helper and the rate limiter.

The behaviour that matters most here is what happens when Redis is *broken*,
because that is the case a cache exists to survive rather than to depend on.
"""

from __future__ import annotations

import json

import pytest

from retailpulse_common.cache import (
    CacheKey,
    InMemoryCache,
    NullCache,
    cached,
    invalidate,
    invalidate_prefix,
)
from retailpulse_common.rate_limit import InMemoryRateLimiter, NoopRateLimiter


@pytest.fixture()
def cache() -> InMemoryCache:
    return InMemoryCache()


class Counter:
    """Loader that records how often it was actually called."""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


# ---------------------------------------------------------------------------
# Cache-aside
# ---------------------------------------------------------------------------
def test_miss_calls_the_loader_and_stores_the_value(cache):
    loader = Counter({"name": "Widget"})

    result = cached(cache, "product:1", loader, ttl_seconds=60)

    assert result == {"name": "Widget"}
    assert loader.calls == 1
    assert cache.get("product:1") is not None


def test_hit_does_not_call_the_loader(cache):
    loader = Counter({"name": "Widget"})
    cached(cache, "product:1", loader, ttl_seconds=60)

    result = cached(cache, "product:1", loader, ttl_seconds=60)

    assert result == {"name": "Widget"}
    assert loader.calls == 1, "the expensive path ran on a hit"


def test_ttl_is_applied(cache):
    cached(cache, "product:1", Counter({"a": 1}), ttl_seconds=120)
    assert cache.ttls["product:1"] == 120


def test_none_is_not_cached(cache):
    """Caching a 'not found' would hide a record created moments later."""
    loader = Counter(None)

    cached(cache, "product:missing", loader, ttl_seconds=60)
    cached(cache, "product:missing", loader, ttl_seconds=60)

    assert loader.calls == 2
    assert cache.get("product:missing") is None


def test_a_poisoned_entry_is_discarded_rather_than_raising(cache):
    """A schema change or truncated write must not break the request."""
    cache.set("product:1", "{not valid json", 60)
    loader = Counter({"name": "Widget"})

    result = cached(cache, "product:1", loader, ttl_seconds=60)

    assert result == {"name": "Widget"}
    assert loader.calls == 1
    assert json.loads(cache.get("product:1")) == {"name": "Widget"}


def test_invalidation_forces_the_next_read_to_reload(cache):
    loader = Counter({"price": "10.00"})
    cached(cache, "product:1", loader, ttl_seconds=60)

    invalidate(cache, "product:1")
    cached(cache, "product:1", loader, ttl_seconds=60)

    assert loader.calls == 2


def test_prefix_invalidation_clears_only_the_matching_keys(cache):
    cache.set("products:category:electronics:1:20", "[]", 60)
    cache.set("products:category:electronics:2:20", "[]", 60)
    cache.set("products:category:groceries:1:20", "[]", 60)
    cache.set("product:123", "{}", 60)

    removed = invalidate_prefix(cache, "products:category:electronics:")

    assert removed == 2
    assert cache.keys() == ["product:123", "products:category:groceries:1:20"]


def test_invalidating_a_missing_key_is_harmless(cache):
    assert invalidate(cache, "product:never-existed") == 0


# ---------------------------------------------------------------------------
# Degrading when Redis is broken
# ---------------------------------------------------------------------------
class BrokenCache:
    """Every operation fails, the way a real Redis outage looks."""

    def get(self, key):
        raise ConnectionError("redis is down")

    def set(self, key, value, ttl_seconds):
        raise ConnectionError("redis is down")

    def delete(self, *keys):
        raise ConnectionError("redis is down")

    def delete_prefix(self, prefix):
        raise ConnectionError("redis is down")

    def ping(self):
        return False


def test_redis_backend_never_propagates_failures(monkeypatch):
    """RedisCache swallows and logs; the request still succeeds."""
    from retailpulse_common.cache import RedisCache

    redis_cache = RedisCache.__new__(RedisCache)
    redis_cache._redis = BrokenCache()
    redis_cache.service_name = "test"

    # None of these may raise.
    assert redis_cache.get("k") is None
    redis_cache.set("k", "v", 60)
    assert redis_cache.delete("k") == 0
    assert redis_cache.delete_prefix("p") == 0
    assert redis_cache.ping() is False


def test_a_cache_outage_still_serves_reads_from_the_loader():
    """The whole point: a cache outage costs latency, not availability."""
    from retailpulse_common.cache import RedisCache

    redis_cache = RedisCache.__new__(RedisCache)
    redis_cache._redis = BrokenCache()
    redis_cache.service_name = "test"

    loader = Counter({"name": "Widget"})
    result = cached(redis_cache, "product:1", loader, ttl_seconds=60)

    assert result == {"name": "Widget"}


def test_null_cache_always_misses():
    loader = Counter({"a": 1})
    cache = NullCache()

    cached(cache, "k", loader, ttl_seconds=60)
    cached(cache, "k", loader, ttl_seconds=60)

    assert loader.calls == 2


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------
def test_keys_follow_the_documented_scheme():
    assert CacheKey.product("abc") == "product:abc"
    assert CacheKey.inventory("p1", "l1") == "inventory:p1:l1"
    assert CacheKey.category_list() == "categories:all"


def test_sku_keys_are_case_insensitive():
    assert CacheKey.product_by_sku("sku-1") == CacheKey.product_by_sku("SKU-1")


def test_category_keys_sit_under_their_invalidation_prefix():
    """If these ever drift apart, invalidation silently stops working."""
    key = CacheKey.products_by_category("Electronics", 1, 20)
    assert key.startswith(CacheKey.products_category_prefix("Electronics"))


def test_inventory_keys_sit_under_their_invalidation_prefix():
    key = CacheKey.inventory("p1", "l1")
    assert key.startswith(CacheKey.inventory_prefix("p1"))


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_requests_under_the_limit_are_allowed():
    limiter = InMemoryRateLimiter()

    for _ in range(5):
        assert limiter.check("user:1", limit=5, window_seconds=60).allowed


def test_the_request_over_the_limit_is_rejected():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        limiter.check("user:1", limit=5, window_seconds=60)

    result = limiter.check("user:1", limit=5, window_seconds=60)

    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after_seconds > 0


def test_remaining_counts_down():
    limiter = InMemoryRateLimiter()

    assert limiter.check("user:1", limit=3, window_seconds=60).remaining == 2
    assert limiter.check("user:1", limit=3, window_seconds=60).remaining == 1
    assert limiter.check("user:1", limit=3, window_seconds=60).remaining == 0


def test_identities_are_limited_independently():
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        limiter.check("user:1", limit=3, window_seconds=60)

    assert limiter.check("user:2", limit=3, window_seconds=60).allowed


def test_the_window_slides():
    """A fixed window would let a caller burst across the boundary."""
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])

    for _ in range(3):
        assert limiter.check("user:1", limit=3, window_seconds=60).allowed
    assert not limiter.check("user:1", limit=3, window_seconds=60).allowed

    now[0] += 61  # the earlier hits have aged out
    assert limiter.check("user:1", limit=3, window_seconds=60).allowed


def test_older_hits_expire_individually_not_all_at_once():
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])

    limiter.check("user:1", limit=2, window_seconds=60)
    now[0] += 30
    limiter.check("user:1", limit=2, window_seconds=60)
    assert not limiter.check("user:1", limit=2, window_seconds=60).allowed

    now[0] += 31  # only the first hit has aged out
    assert limiter.check("user:1", limit=2, window_seconds=60).allowed
    assert not limiter.check("user:1", limit=2, window_seconds=60).allowed


def test_headers_are_present_on_allowed_requests_too():
    """So a client can slow down before it is ever rejected."""
    headers = InMemoryRateLimiter().check("user:1", limit=10, window_seconds=60).headers()

    assert headers["X-RateLimit-Limit"] == "10"
    assert headers["X-RateLimit-Remaining"] == "9"
    assert "Retry-After" not in headers


def test_retry_after_is_only_sent_on_rejection():
    limiter = InMemoryRateLimiter()
    limiter.check("user:1", limit=1, window_seconds=60)

    headers = limiter.check("user:1", limit=1, window_seconds=60).headers()

    assert "Retry-After" in headers
    assert int(headers["Retry-After"]) > 0


def test_reset_clears_a_callers_window():
    limiter = InMemoryRateLimiter()
    limiter.check("user:1", limit=1, window_seconds=60)

    limiter.reset("user:1")

    assert limiter.check("user:1", limit=1, window_seconds=60).allowed


def test_redis_limiter_fails_open_when_redis_is_unavailable():
    """A deliberate trade: availability over strictness. See rate_limit.py."""
    from retailpulse_common.rate_limit import RedisRateLimiter

    limiter = RedisRateLimiter.__new__(RedisRateLimiter)

    def _explode(**kwargs):
        raise ConnectionError("redis is down")

    limiter._script = _explode

    result = limiter.check("user:1", limit=1, window_seconds=60)

    assert result.allowed is True
    assert result.remaining == 1


def test_noop_limiter_allows_everything():
    limiter = NoopRateLimiter()
    for _ in range(1000):
        assert limiter.check("user:1", limit=1, window_seconds=60).allowed
