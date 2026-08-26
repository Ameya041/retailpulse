"""Product service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import cache_ready, database_ready
from app.routes import category_router, router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Product Service",
    description=(
        "Owns the product catalog: products, categories, search and pagination.\n\n"
        "**Data ownership.** This service is the only writer of the `products` and "
        "`categories` tables. Other services reference a `product_id` and read "
        "through this API rather than querying the catalog database directly.\n\n"
        "**Caching.** Single-product lookups and the category list are cached in Redis "
        "with cache-aside semantics: read through on a miss, and *delete* (never "
        "overwrite) on a write, because overwriting races under concurrent updates. "
        "Redis is never the source of truth -- if it is unavailable every read falls "
        "back to Postgres, so a cache outage costs latency rather than availability."
    ),
    checks={"database": database_ready, "cache": cache_ready},
)

app.include_router(router)
app.include_router(category_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
