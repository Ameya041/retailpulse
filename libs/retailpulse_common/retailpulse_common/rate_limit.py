"""Redis-backed rate limiting.

## Why a sliding window

A **fixed window** ("100 requests per calendar minute") is trivial to implement
with INCR + EXPIRE, and it lets a caller send 200 requests in two seconds: 100
at 11:59:59 and 100 more at 12:00:00. The limit is nominally respected and the
service is still hit at twice the intended rate.

A **sliding window log** keeps a timestamp per request in a sorted set and is
exact, at the cost of storing every request. At 100 req/min per user that is
fine; it would not be at 100k.

This uses the sliding window log, because the accuracy matters more than the
memory at this scale and the implementation is short enough to be obviously
correct.

## Why Lua

Check-then-increment is a read followed by a write. Two concurrent requests can
both read 99, both decide they are under the limit, and both write -- so the
101st request succeeds. Redis executes a Lua script atomically, so the count,
the decision and the insert happen as one indivisible step. This is the same
class of bug as the inventory oversell, and it has the same shape of fix.

## Fail-open, deliberately

If Redis is unavailable the limiter **allows** the request. That is a real
security trade-off and worth stating plainly: an attacker who can take Redis
down can also bypass rate limiting. The alternative -- failing closed -- means a
Redis outage takes the entire API offline, converting a degraded cache into a
total outage. For a retail storefront, availability wins. A system where the
limiter is the primary defence against abuse (rather than one layer of several)
should choose differently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# KEYS[1] = the window key
# ARGV[1] = now (ms), ARGV[2] = window (ms), ARGV[3] = limit, ARGV[4] = member id
#
# Returns {allowed, count, oldest_ms}. Executed atomically by Redis, so the
# count and the insert cannot interleave with another request.
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

-- Drop entries that have fallen out of the window.
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local count = redis.call('ZCARD', key)

if count >= limit then
  -- Report when the oldest entry expires, so the caller can be told exactly
  -- how long to wait rather than being made to guess.
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local oldest_ms = now
  if oldest[2] then oldest_ms = tonumber(oldest[2]) end
  return {0, count, oldest_ms}
end

redis.call('ZADD', key, now, member)
-- Expire the key itself so idle callers do not leak memory forever.
redis.call('PEXPIRE', key, window)
return {1, count + 1, now}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    def headers(self) -> dict[str, str]:
        """Standard rate-limit headers, sent on every response.

        Included on allowed requests too, so a well-behaved client can slow
        itself down before it is ever rejected.
        """
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class RateLimiter(Protocol):
    def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitResult: ...


class RedisRateLimiter:
    def __init__(self, url: str, *, socket_timeout: float = 0.25) -> None:
        import redis

        # Tighter timeout than the cache: this runs on every single request,
        # and a slow limiter would add latency to the whole API.
        self._redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
        )
        self._script = self._redis.register_script(SLIDING_WINDOW_LUA)

    def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        key = f"ratelimit:{identity}"
        # Unique member per request; two requests in the same millisecond must
        # not collapse into one sorted-set entry.
        member = f"{now_ms}-{time.perf_counter_ns()}"

        try:
            allowed, count, oldest_ms = self._script(
                keys=[key], args=[now_ms, window_ms, limit, member]
            )
        except Exception as exc:
            # Fail open. See the module docstring for why.
            logger.warning(
                "rate limiter unavailable; allowing the request",
                extra={"identity": identity, "error": str(exc)},
            )
            return RateLimitResult(
                allowed=True, limit=limit, remaining=limit, retry_after_seconds=0
            )

        allowed = bool(allowed)
        retry_after = 0
        if not allowed:
            elapsed_ms = now_ms - int(oldest_ms)
            retry_after = max(1, (window_ms - elapsed_ms + 999) // 1000)

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=limit - int(count),
            retry_after_seconds=int(retry_after),
        )

    def reset(self, identity: str) -> None:
        """Clear a caller's window. Used by tests and by support tooling."""
        try:
            self._redis.delete(f"ratelimit:{identity}")
        except Exception:
            logger.warning("rate limit reset failed", extra={"identity": identity})

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False


class InMemoryRateLimiter:
    """Test double implementing the same sliding-window semantics.

    Single-process only -- which is exactly why production uses Redis: with
    several API replicas, a per-process counter would let each replica grant
    the full limit independently.
    """

    def __init__(self, clock=time.time) -> None:
        self._hits: dict[str, list[float]] = {}
        self._clock = clock

    def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now = self._clock()
        cutoff = now - window_seconds
        hits = [t for t in self._hits.get(identity, []) if t > cutoff]

        if len(hits) >= limit:
            self._hits[identity] = hits
            retry_after = max(1, int(hits[0] + window_seconds - now) + 1)
            return RateLimitResult(
                allowed=False, limit=limit, remaining=0, retry_after_seconds=retry_after
            )

        hits.append(now)
        self._hits[identity] = hits
        return RateLimitResult(
            allowed=True, limit=limit, remaining=limit - len(hits), retry_after_seconds=0
        )

    def reset(self, identity: str) -> None:
        self._hits.pop(identity, None)

    def ping(self) -> bool:
        return True


class NoopRateLimiter:
    """Allows everything. Used when rate limiting is switched off."""

    def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitResult:  # noqa: ARG002
        return RateLimitResult(allowed=True, limit=limit, remaining=limit, retry_after_seconds=0)

    def reset(self, identity: str) -> None:  # noqa: ARG002
        return None

    def ping(self) -> bool:
        return True
