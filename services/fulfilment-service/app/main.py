"""Fulfilment service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
from app.routes import router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Fulfilment Service",
    description=(
        "Shipments and delivery tracking.\n\n"
        "**Where reversibility ends.** Up to CONFIRMED an order is rows in "
        "databases and can be cancelled by writing different rows. Once a parcel "
        "leaves a warehouse there is a physical object in a van, so the fulfilment "
        "state machine has no cancellation edge at all -- undoing a shipment is a "
        "returns process, not a status change.\n\n"
        "**One shipment per order.** A unique index on `fulfilments.order_id` means "
        "a redelivered ORDER_CONFIRMED cannot dispatch the same goods twice.\n\n"
        "**Bounded delivery attempts.** After three failures the parcel stays in "
        "FAILED_DELIVERY rather than being retried forever; at that point it needs "
        "a human, not another van."
    ),
    checks={"database": database_ready},
)

app.include_router(router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
