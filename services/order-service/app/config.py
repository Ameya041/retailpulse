"""Order service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class OrderSettings(ServiceSettings):
    service_name: str = "order-service"
    db_name: str = "retailpulse_order"
    port: int = 8003

    product_service_url: str = "http://localhost:8001"
    inventory_service_url: str = "http://localhost:8002"


@lru_cache(maxsize=1)
def get_settings() -> OrderSettings:
    return OrderSettings()
