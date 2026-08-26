"""Caching layer for the product catalog.

Kept separate from ``service.py`` so the domain logic has no idea a cache
exists. That matters for two reasons: the service stays testable without Redis,
and the Kafka consumer path reuses the same service without accidentally
serving stale reads.

**What is cached.** Single-product lookups and the category list. These are
read constantly and change rarely -- the definition of a good cache candidate.

**What is not cached.** Search results and paginated listings. Search has an
unbounded key space (every distinct query string is a new key), so the hit rate
would be poor while the memory cost is real. Paginated lists go stale the
moment any product in them changes, and invalidating them precisely means
tracking which pages contain which product, which costs more than it saves.
"""

from __future__ import annotations

import json
import logging
import uuid

from retailpulse_common.cache import (
    CacheBackend,
    CacheKey,
    cached,
    invalidate,
    invalidate_prefix,
)

logger = logging.getLogger("product-service")

SERVICE = "product-service"


def read_product(
    cache: CacheBackend, product_id: uuid.UUID, loader, ttl_seconds: int
) -> dict:
    """Cache-aside read of a single product, keyed by ``product:{id}``."""
    return cached(
        cache,
        CacheKey.product(product_id),
        loader,
        ttl_seconds=ttl_seconds,
        service_name=SERVICE,
    )


def read_product_by_sku(cache: CacheBackend, sku: str, loader, ttl_seconds: int) -> dict:
    return cached(
        cache,
        CacheKey.product_by_sku(sku),
        loader,
        ttl_seconds=ttl_seconds,
        service_name=SERVICE,
    )


def read_categories(cache: CacheBackend, loader, ttl_seconds: int) -> list:
    return cached(
        cache,
        CacheKey.category_list(),
        loader,
        ttl_seconds=ttl_seconds,
        service_name=SERVICE,
    )


def invalidate_product(
    cache: CacheBackend, product_id: uuid.UUID, sku: str, category: str | None = None
) -> None:
    """Drop every key that could serve a stale copy of this product.

    Both the id key and the SKU key are removed: they are two doors onto the
    same row, and clearing only one leaves the other serving the old value.
    """
    keys = [CacheKey.product(product_id), CacheKey.product_by_sku(sku)]
    invalidate(cache, *keys, service_name=SERVICE)

    if category:
        # Any cached page of this category may now be wrong.
        invalidate_prefix(
            cache, CacheKey.products_category_prefix(category), service_name=SERVICE
        )

    logger.debug("product cache invalidated", extra={"product_id": str(product_id)})


def invalidate_categories(cache: CacheBackend) -> None:
    invalidate(cache, CacheKey.category_list(), service_name=SERVICE)


def dumps(value) -> str:
    return json.dumps(value, default=str)
