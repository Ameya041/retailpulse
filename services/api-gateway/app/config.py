"""API gateway configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from retailpulse_common.config import ServiceSettings


class GatewaySettings(ServiceSettings):
    service_name: str = "api-gateway"
    port: int = 8000

    # The gateway owns no database. Listed explicitly because a reader will
    # otherwise wonder where its db_name went.
    db_name: str = "unused"

    product_service_url: str = "http://localhost:8001"
    inventory_service_url: str = "http://localhost:8002"
    order_service_url: str = "http://localhost:8003"
    user_service_url: str = "http://localhost:8004"
    payment_service_url: str = "http://localhost:8005"
    fulfilment_service_url: str = "http://localhost:8006"
    analytics_service_url: str = "http://localhost:8007"
    ml_service_url: str = "http://localhost:8008"

    # Must be shorter than any client-side timeout, so the gateway is the one
    # that gives up first and can return a clean 504 rather than the client
    # seeing a dropped connection.
    upstream_timeout_seconds: float = 10.0

    # Anonymous callers share an IP-based bucket and get a smaller allowance
    # than an authenticated user, who is individually identifiable.
    rate_limit_requests_per_minute: int = 100
    anonymous_rate_limit_per_minute: int = 30
    rate_limit_window_seconds: int = 60
    rate_limit_enabled: bool = True

    circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    circuit_breaker_cool_down_seconds: float = Field(default=15.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> GatewaySettings:
    return GatewaySettings()
