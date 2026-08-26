"""Payment service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
from app.routes import router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Payment Service",
    description=(
        "Simulated payment processing. No real money moves; what is simulated is "
        "the part that matters architecturally -- providers decline, and sometimes "
        "they are unreachable.\n\n"
        "**One charge per order.** A unique index on `payments.order_id` means a "
        "redelivered PAYMENT_REQUESTED can never charge a customer twice. The "
        "idempotency table stops the duplicate first; the unique index is the "
        "backstop, because when the failure mode is double-charging, the guarantee "
        "belongs in the database rather than in application logic.\n\n"
        "**Declines are not errors.** A decline is an answer and is published as "
        "PAYMENT_FAILED. An unreachable provider is not an answer, so it is retried "
        "with backoff. Conflating the two gives either infinite retries on a "
        "declined card, or an outage reported to the customer as a refusal."
    ),
    checks={"database": database_ready},
)

app.include_router(router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
