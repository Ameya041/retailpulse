"""Analytics service wiring."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session

from app.clients import (
    ForecastClient,
    HttpForecastClient,
    HttpInventoryClient,
    InventoryClient,
)
from app.config import get_settings
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
def _inventory_client() -> HttpInventoryClient:
    return HttpInventoryClient(
        settings.inventory_service_url, settings.downstream_timeout_seconds
    )


@lru_cache(maxsize=1)
def _forecast_client() -> HttpForecastClient:
    return HttpForecastClient(settings.ml_service_url, settings.downstream_timeout_seconds)


def get_inventory_client() -> InventoryClient:
    """Overridden in tests with a stub."""
    return _inventory_client()


def get_forecast_client() -> ForecastClient:
    return _forecast_client()


current_user, optional_user, require_roles = build_auth_dependencies(
    settings.jwt_secret_key, settings.jwt_algorithm
)


def database_ready() -> bool:
    return get_database().ping()
