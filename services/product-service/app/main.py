"""Product service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
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
        "through this API rather than querying the catalog database directly."
    ),
    checks={"database": database_ready},
)

app.include_router(router)
app.include_router(category_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
