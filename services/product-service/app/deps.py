"""Wiring: database handle, session dependency and auth dependencies.

Kept in one module so tests can override a single dependency
(``get_db_session``) and swap Postgres for an in-memory SQLite database.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session

from app.config import get_settings
from app.service import ProductService
from retailpulse_common.auth import build_auth_dependencies
from retailpulse_common.cache import CacheBackend, NullCache, RedisCache
from retailpulse_common.db import Database

settings = get_settings()


@lru_cache(maxsize=1)
def get_database() -> Database:
    return Database(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
    )


def get_db_session() -> Iterator[Session]:
    yield from get_database().get_session()


def get_product_service(session: Session) -> ProductService:
    return ProductService(session)


@lru_cache(maxsize=1)
def _cache() -> CacheBackend:
    if not settings.redis_url:
        return NullCache()
    return RedisCache(settings.redis_url, service_name=settings.service_name)


def get_cache() -> CacheBackend:
    """Overridden in tests with an in-memory cache."""
    return _cache()


def cache_ready() -> bool:
    """Reported by /ready but never fatal.

    Redis being down degrades performance, not correctness -- every cached
    value can be rebuilt from Postgres. The readiness endpoint surfaces it so
    it is visible in monitoring without pulling the pod out of the load
    balancer for a non-essential dependency.
    """
    return _cache().ping()


current_user, optional_user, require_roles = build_auth_dependencies(
    settings.jwt_secret_key, settings.jwt_algorithm
)


def database_ready() -> bool:
    return get_database().ping()
