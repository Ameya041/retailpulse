"""Order service wiring."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session

from app.config import get_settings
from app.product_client import HttpProductCatalog, ProductCatalog
from retailpulse_common.auth import build_auth_dependencies
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


@lru_cache(maxsize=1)
def _catalog() -> HttpProductCatalog:
    return HttpProductCatalog(
        settings.product_service_url, timeout_seconds=settings.http_client_timeout_seconds
    )


def get_catalog() -> ProductCatalog:
    """Overridden in tests with an in-memory catalog."""
    return _catalog()


current_user, optional_user, require_roles = build_auth_dependencies(
    settings.jwt_secret_key, settings.jwt_algorithm
)


def database_ready() -> bool:
    return get_database().ping()
