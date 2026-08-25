"""Inventory service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class InventorySettings(ServiceSettings):
    service_name: str = "inventory-service"
    db_name: str = "retailpulse_inventory"
    port: int = 8002

    # Where to reach the catalog when validating that a product exists.
    product_service_url: str = "http://localhost:8001"


@lru_cache(maxsize=1)
def get_settings() -> InventorySettings:
    return InventorySettings()
