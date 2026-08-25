"""Product service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class ProductSettings(ServiceSettings):
    service_name: str = "product-service"
    db_name: str = "retailpulse_product"
    port: int = 8001


@lru_cache(maxsize=1)
def get_settings() -> ProductSettings:
    return ProductSettings()
