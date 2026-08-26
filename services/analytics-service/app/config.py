"""Analytics service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class AnalyticsSettings(ServiceSettings):
    service_name: str = "analytics-service"
    db_name: str = "retailpulse_analytics"
    port: int = 8007

    inventory_service_url: str = "http://localhost:8002"
    ml_service_url: str = "http://localhost:8008"

    # Short, because these calls enrich a dashboard rather than serve it. A
    # slow dependency should cost a missing tile, not a hung page.
    downstream_timeout_seconds: float = 3.0


@lru_cache(maxsize=1)
def get_settings() -> AnalyticsSettings:
    return AnalyticsSettings()
