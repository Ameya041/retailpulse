"""Payment service configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from retailpulse_common.config import ServiceSettings


class PaymentSettings(ServiceSettings):
    service_name: str = "payment-service"
    db_name: str = "retailpulse_payment"
    port: int = 8005

    # Proportion of charges the simulated gateway approves. Configurable so the
    # compensation path can be demonstrated on demand: set it to 0.0 and every
    # order walks the payment-failure branch.
    payment_success_rate: float = Field(default=0.95, ge=0.0, le=1.0)

    # Fixing the seed makes a demo reproducible. Left unset in normal running.
    payment_rng_seed: int | None = None


@lru_cache(maxsize=1)
def get_settings() -> PaymentSettings:
    return PaymentSettings()
