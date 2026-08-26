"""Fulfilment service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class FulfilmentSettings(ServiceSettings):
    service_name: str = "fulfilment-service"
    db_name: str = "retailpulse_fulfilment"
    port: int = 8006

    # Fixing the seed makes carriers and tracking numbers reproducible in a demo.
    fulfilment_rng_seed: int | None = None


@lru_cache(maxsize=1)
def get_settings() -> FulfilmentSettings:
    return FulfilmentSettings()
