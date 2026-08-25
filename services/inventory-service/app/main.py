"""Inventory service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
from app.routes import location_router, router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Inventory Service",
    description=(
        "Owns stock across every location, and the transaction-safe reservation "
        "lifecycle: reserve -> (release | commit).\n\n"
        "**Consistency.** Quantity changes take a row-level `SELECT ... FOR UPDATE` "
        "lock inside a single transaction, so concurrent orders for the last unit "
        "serialise rather than oversell. Locks are always acquired in `inventory_id` "
        "order, which makes deadlock between multi-line orders impossible. CHECK "
        "constraints on `available_quantity` and `reserved_quantity` are the backstop.\n\n"
        "**Idempotency.** A unique index on `(order_id, product_id, location_id)` means "
        "a redelivered ORDER_CREATED event cannot reserve the same stock twice."
    ),
    checks={"database": database_ready},
)

app.include_router(router)
app.include_router(location_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
