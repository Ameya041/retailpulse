"""Clients for the services analytics enriches its dashboard from.

Every call here is **optional**. Analytics owns sales facts; inventory value
and forecasts belong to other services. If one of them is down the dashboard
should lose a tile, not fail to render -- so these clients return ``None`` on
failure rather than raising, and the API reports the gap explicitly instead of
substituting a zero.

Reporting an inventory value of zero during an outage would be worse than
reporting nothing: someone would act on it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Protocol

import httpx

logger = logging.getLogger("analytics-service")


class InventoryClient(Protocol):
    def summary(self) -> dict | None: ...


class ForecastClient(Protocol):
    def replenishment(self, product_id: str, store_id: str, current_stock: int, threshold: int) -> dict | None: ...


class HttpInventoryClient:
    def __init__(self, base_url: str, timeout_seconds: float = 3.0, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def summary(self) -> dict | None:
        try:
            response = httpx.get(
                f"{self.base_url}/inventory/low-stock",
                params={"limit": 500},
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            low_stock = response.json()
        except httpx.HTTPError as exc:
            logger.warning("inventory service unavailable", extra={"error": str(exc)})
            return None

        return {
            "low_stock_products": len(low_stock),
            # Inventory value needs product prices, which live in the catalog.
            # Left as None rather than guessed; the dashboard shows it as
            # unavailable, which is the truth.
            "inventory_value": None,
        }


class HttpForecastClient:
    def __init__(self, base_url: str, timeout_seconds: float = 3.0, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.token = token

    def replenishment(
        self, product_id: str, store_id: str, current_stock: int, threshold: int
    ) -> dict | None:
        try:
            response = httpx.post(
                f"{self.base_url}/forecast/replenishment",
                json={
                    "product_id": product_id,
                    "store_id": store_id,
                    "current_stock": current_stock,
                    "reorder_threshold": threshold,
                },
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            logger.warning(
                "ml service unavailable",
                extra={"product_id": product_id, "error": str(exc)},
            )
            return None


class NullInventoryClient:
    """Used when the dependency is not configured, and by tests."""

    def summary(self) -> dict | None:
        return None


class NullForecastClient:
    def replenishment(self, product_id, store_id, current_stock, threshold) -> dict | None:  # noqa: ARG002
        return None


class StubInventoryClient:
    """Test double returning fixed values."""

    def __init__(self, low_stock_products: int = 3, inventory_value: Decimal | None = None):
        self._summary = {
            "low_stock_products": low_stock_products,
            "inventory_value": inventory_value,
        }

    def summary(self) -> dict | None:
        return self._summary
