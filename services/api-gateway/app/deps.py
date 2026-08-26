"""API gateway wiring."""

from __future__ import annotations

from functools import lru_cache

import httpx

from app.config import get_settings
from app.routing import BreakerRegistry
from retailpulse_common.auth import build_auth_dependencies
from retailpulse_common.rate_limit import (
    NoopRateLimiter,
    RateLimiter,
    RedisRateLimiter,
)

settings = get_settings()


@lru_cache(maxsize=1)
def get_http_client() -> httpx.AsyncClient:
    """One shared client for the whole process.

    Creating a client per request would open a new TCP connection every time
    and throw away connection pooling and keep-alive -- which, on the hot path
    of every API call, is a large and entirely avoidable cost.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout_seconds),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
    )


@lru_cache(maxsize=1)
def get_breakers() -> BreakerRegistry:
    return BreakerRegistry(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        cool_down_seconds=settings.circuit_breaker_cool_down_seconds,
    )


@lru_cache(maxsize=1)
def _rate_limiter() -> RateLimiter:
    if not settings.rate_limit_enabled or not settings.redis_url:
        return NoopRateLimiter()
    return RedisRateLimiter(settings.redis_url)


def get_rate_limiter() -> RateLimiter:
    """Overridden in tests with an in-memory limiter."""
    return _rate_limiter()


current_user, optional_user, require_roles = build_auth_dependencies(
    settings.jwt_secret_key, settings.jwt_algorithm
)


def rate_limiter_ready() -> bool:
    limiter = _rate_limiter()
    return limiter.ping() if hasattr(limiter, "ping") else True


async def close_http_client() -> None:
    await get_http_client().aclose()
