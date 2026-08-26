"""Order service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
from app.routes import cart_router, order_router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Order Service",
    description=(
        "Carts, orders, order lines and the order state machine.\n\n"
        "**State machine.** Status is a node in an explicit transition graph, not a "
        "free-text field. Every change goes through one validation layer, so an illegal "
        "move such as DELIVERED -> CREATED is rejected with 409 rather than silently "
        "applied. DELIVERED and CANCELLED are absorbing states, which is what makes a "
        "late duplicate event harmless.\n\n"
        "**Pricing.** Order lines snapshot the catalog price at order time. A later price "
        "change cannot alter a placed order's total.\n\n"
        "**Ownership.** Customers see only their own orders; requesting another "
        "customer's order returns 404 rather than 403, so order IDs cannot be probed."
    ),
    checks={"database": database_ready},
)

app.include_router(cart_router)
app.include_router(order_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
