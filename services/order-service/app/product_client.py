"""Client for the product catalog.

The order service must not read the catalog's database directly -- that would
couple the two services at the schema level and make either one impossible to
deploy independently. It calls the API instead.

Two failure-handling rules, because a synchronous call to another service is a
liability:

* **Always a timeout.** A call with no timeout turns the catalog being slow
  into the order service hanging, which turns into the gateway's connection
  pool filling up. One slow dependency should not take down the checkout path.
* **Fail closed.** If the catalog cannot be reached, order creation is
  rejected with 503 rather than guessing prices. Charging the wrong amount is
  far worse than asking the customer to retry.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx

from retailpulse_common.errors import ServiceUnavailableError

logger = logging.getLogger("order-service")


@dataclass(frozen=True)
class CatalogProduct:
    """The subset of a product the order service actually needs."""

    product_id: uuid.UUID
    sku: str
    name: str
    price: Decimal
    currency: str
    status: str

    @property
    def is_orderable(self) -> bool:
        return self.status == "ACTIVE"


class ProductCatalog(Protocol):
    """Interface the order service depends on.

    A Protocol rather than the concrete client so tests can substitute an
    in-memory catalog without patching HTTP internals.
    """

    def get_many(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogProduct]:
        ...


class HttpProductCatalog:
    """Talks to product-service over HTTP."""

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_many(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogProduct]:
        """Fetch several products in one request.

        One bulk call rather than N calls: a 10-line order should cost one
        network round trip, not ten.
        """
        if not product_ids:
            return {}

        try:
            response = httpx.post(
                f"{self.base_url}/products/bulk",
                json={"product_ids": [str(pid) for pid in product_ids]},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            logger.warning("catalog timeout", extra={"count": len(product_ids)})
            raise ServiceUnavailableError(
                "The product catalog did not respond in time. Please retry."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("catalog unreachable", extra={"error": str(exc)})
            raise ServiceUnavailableError(
                "The product catalog is unavailable. Please retry."
            ) from exc

        products: dict[uuid.UUID, CatalogProduct] = {}
        for row in payload:
            product = CatalogProduct(
                product_id=uuid.UUID(row["product_id"]),
                sku=row["sku"],
                name=row["name"],
                # Parse from the string the API returns -- going via float
                # would reintroduce the rounding error NUMERIC exists to avoid.
                price=Decimal(str(row["price"])),
                currency=row["currency"],
                status=row["status"],
            )
            products[product.product_id] = product
        return products


class InMemoryProductCatalog:
    """Test double. Also used by the local seeder."""

    def __init__(self, products: dict[uuid.UUID, CatalogProduct] | None = None) -> None:
        self._products = products or {}

    def add(self, product: CatalogProduct) -> None:
        self._products[product.product_id] = product

    def get_many(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogProduct]:
        return {pid: self._products[pid] for pid in product_ids if pid in self._products}
