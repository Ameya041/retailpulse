"""Redis cache-aside helper.

## The strategy: cache-aside (lazy loading)

    read  -> look in Redis
          -> hit:  return it
          -> miss: read Postgres, write to Redis, return it
    write -> write Postgres, then DELETE the key

Two choices in there are worth defending.

**Delete on write, never update.** Writing the new value into the cache looks
more efficient but races: two concurrent updates can land in Postgres in one
order and in Redis in the other, leaving the cache permanently disagreeing with
the database. A delete has no such ordering problem -- the next read repopulates
from whatever the database actually holds. The cost is one extra miss.

**Cache-aside rather than write-through.** Write-through keeps the cache warm
but couples every write to Redis being available, and pays to cache data that
may never be read. For a catalog where reads vastly outnumber writes, and where
most products are never fetched on a given day, lazy population is the better
trade.

## Redis is never the source of truth

Every value here can be rebuilt from Postgres. That is what makes the failure
policy safe: **if Redis is unavailable, every operation degrades to a direct
database read** rather than raising. A cache outage should make the system
slower, not broken. The alternative -- propagating the error -- turns an
optional dependency into a mandatory one, which defeats the point of having it.

## TTLs

Every key has one. Invalidation is explicit on write, so a TTL is not the
primary correctness mechanism -- it is the backstop for the case that a delete
was missed (a crash between the Postgres commit and the Redis delete). It
bounds how long any such staleness can survive.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from retailpulse_common.observability import CACHE_OPERATIONS_TOTAL

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TTL_SECONDS = 300


class CacheBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def delete(self, *keys: str) -> int: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------
class CacheKey:
    """Central key registry.

    Keys are built here rather than formatted at call sites so that an
    invalidation and the read it is meant to invalidate cannot drift apart --
    a stale cache caused by a mistyped key is close to undebuggable.
    """

    @staticmethod
    def product(product_id: Any) -> str:
        return f"product:{product_id}"

    @staticmethod
    def product_by_sku(sku: str) -> str:
        return f"product:sku:{sku.upper()}"

    @staticmethod
    def products_by_category(category: str, page: int, page_size: int) -> str:
        return f"products:category:{category.lower()}:{page}:{page_size}"

    @staticmethod
    def products_category_prefix(category: str) -> str:
        return f"products:category:{category.lower()}:"

    @staticmethod
    def category_list() -> str:
        return "categories:all"

    @staticmethod
    def inventory(product_id: Any, location_id: Any) -> str:
        return f"inventory:{product_id}:{location_id}"

    @staticmethod
    def inventory_summary(product_id: Any) -> str:
        return f"inventory:{product_id}:summary"

    @staticmethod
    def inventory_prefix(product_id: Any) -> str:
        return f"inventory:{product_id}:"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class RedisCache:
    """Redis-backed cache that never propagates Redis failures."""

    def __init__(self, url: str, *, service_name: str = "unknown", socket_timeout: float = 0.5):
        import redis

        # A short timeout is essential. The whole point of the cache is to be
        # faster than Postgres; a cache lookup that blocks for seconds is worse
        # than no cache at all.
        self._redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            health_check_interval=30,
        )
        self.service_name = service_name

    def get(self, key: str) -> str | None:
        try:
            return self._redis.get(key)
        except Exception as exc:
            logger.warning("cache read failed; falling back to the database", extra={"key": key, "error": str(exc)})
            CACHE_OPERATIONS_TOTAL.labels(self.service_name, "error").inc()
            return None

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            self._redis.setex(key, ttl_seconds, value)
        except Exception as exc:
            # A failed write just means the next read is another miss.
            logger.warning("cache write failed", extra={"key": key, "error": str(exc)})
            CACHE_OPERATIONS_TOTAL.labels(self.service_name, "error").inc()

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        try:
            return int(self._redis.delete(*keys))
        except Exception as exc:
            # This one is genuinely dangerous: a missed invalidation leaves a
            # stale value until its TTL expires. Logged at error so it is
            # visible, and the TTL bounds the damage.
            logger.error(
                "cache invalidation failed; value stays stale until its TTL",
                extra={"keys": list(keys), "error": str(exc)},
            )
            CACHE_OPERATIONS_TOTAL.labels(self.service_name, "error").inc()
            return 0

    def delete_prefix(self, prefix: str) -> int:
        """Invalidate every key under a prefix.

        Uses SCAN, never KEYS. KEYS blocks the entire Redis server while it
        walks the keyspace, which on a production instance is an outage.
        """
        try:
            removed = 0
            for batch in self._scan_batches(f"{prefix}*"):
                if batch:
                    removed += int(self._redis.delete(*batch))
            return removed
        except Exception as exc:
            logger.error("prefix invalidation failed", extra={"prefix": prefix, "error": str(exc)})
            CACHE_OPERATIONS_TOTAL.labels(self.service_name, "error").inc()
            return 0

    def _scan_batches(self, match: str, batch_size: int = 200):
        cursor = 0
        while True:
            cursor, keys = self._redis.scan(cursor=cursor, match=match, count=batch_size)
            yield keys
            if cursor == 0:
                return

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False


class InMemoryCache:
    """Test double. Same semantics, no Redis.

    Does not implement TTL expiry -- tests that care about expiry assert on the
    recorded TTL instead of sleeping, which keeps the suite fast.
    """

    def __init__(self, service_name: str = "test") -> None:
        self._data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.service_name = service_name

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = value
        self.ttls[key] = ttl_seconds

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._data.pop(key, None) is not None:
                self.ttls.pop(key, None)
                removed += 1
        return removed

    def delete_prefix(self, prefix: str) -> int:
        matching = [k for k in self._data if k.startswith(prefix)]
        return self.delete(*matching)

    def delete_prefix_count(self, prefix: str) -> int:
        return sum(1 for k in self._data if k.startswith(prefix))

    def ping(self) -> bool:
        return True

    def keys(self) -> list[str]:
        return sorted(self._data)


class NullCache:
    """Disables caching entirely. Used when Redis is not configured."""

    service_name = "null"

    def get(self, key: str) -> str | None:  # noqa: ARG002
        return None

    def set(self, key: str, value: str, ttl_seconds: int) -> None:  # noqa: ARG002
        return None

    def delete(self, *keys: str) -> int:  # noqa: ARG002
        return 0

    def delete_prefix(self, prefix: str) -> int:  # noqa: ARG002
        return 0

    def ping(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Cache-aside
# ---------------------------------------------------------------------------
def cached(
    cache: CacheBackend,
    key: str,
    loader: Callable[[], T],
    *,
    serialize: Callable[[T], str] = lambda v: json.dumps(v, default=str),
    deserialize: Callable[[str], T] = json.loads,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    service_name: str = "unknown",
) -> T:
    """Return a cached value, computing and storing it on a miss.

    ``loader`` is only called on a miss, so the expensive path stays out of the
    hot case entirely.
    """
    raw = _safe(cache.get, key, service_name=service_name, action="read")
    if raw is not None:
        try:
            value = deserialize(raw)
        except Exception:
            # A poisoned entry (bad JSON, changed schema) must not break the
            # request. Drop it and fall through to the loader.
            logger.warning("discarding undecodable cache entry", extra={"key": key})
            _safe(cache.delete, key, service_name=service_name, action="invalidate")
        else:
            CACHE_OPERATIONS_TOTAL.labels(service_name, "hit").inc()
            return value

    CACHE_OPERATIONS_TOTAL.labels(service_name, "miss").inc()
    value = loader()

    # `None` is not cached: it is usually "not found", and caching it would
    # keep a newly-created record invisible for a whole TTL.
    if value is not None:
        _safe(
            cache.set,
            key,
            serialize(value),
            ttl_seconds,
            service_name=service_name,
            action="write",
        )

    return value


def _safe(operation, *args, service_name: str, action: str):
    """Run a cache operation, swallowing backend failures.

    The guarantee that a cache outage degrades rather than breaks belongs
    *here*, at the single point every caller goes through -- not inside each
    backend, where a new or mis-implemented backend would silently opt out of
    it. RedisCache also handles its own errors; this is deliberate defence in
    depth for a promise the whole system relies on.
    """
    try:
        return operation(*args)
    except Exception as exc:  # noqa: BLE001 - a cache must never break a request
        level = logger.error if action == "invalidate" else logger.warning
        level(
            "cache %s failed; continuing without the cache",
            action,
            extra={"error": str(exc), "service": service_name},
        )
        CACHE_OPERATIONS_TOTAL.labels(service_name, "error").inc()
        return None


def invalidate(cache: CacheBackend, *keys: str, service_name: str = "unknown") -> int:
    removed = _safe(cache.delete, *keys, service_name=service_name, action="invalidate") or 0
    if removed:
        CACHE_OPERATIONS_TOTAL.labels(service_name, "invalidate").inc(removed)
    return removed


def invalidate_prefix(cache: CacheBackend, prefix: str, *, service_name: str = "unknown") -> int:
    removed = (
        _safe(cache.delete_prefix, prefix, service_name=service_name, action="invalidate") or 0
    )
    if removed:
        CACHE_OPERATIONS_TOTAL.labels(service_name, "invalidate").inc(removed)
    return removed
