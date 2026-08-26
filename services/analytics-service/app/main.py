"""Analytics service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import database_ready
from app.routes import router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse Analytics Service",
    description=(
        "Sales analytics built from events, not from the transactional databases.\n\n"
        "**A read model.** This service owns no transactional state. It consumes order "
        "events into an append-only `sales_facts` table and rolls those up into "
        "`daily_aggregates`. Answering 'revenue by category' from the order service's "
        "tables would need a cross-service join that cannot exist, and would put "
        "analytical scans on the database serving checkout.\n\n"
        "**Aggregates are recomputed, not incremented.** Incremental counters drift "
        "when an event is replayed or arrives late, and the drift is invisible. "
        "Rebuilding a date range from immutable facts is idempotent by construction.\n\n"
        "**Late events land on the right day.** The business date comes from the "
        "event's own timestamp, never from the processing time, so a replay cannot "
        "shift revenue between days.\n\n"
        "**Degrades honestly.** Inventory value and forecasts belong to other "
        "services. When one is unreachable the response reports null and says why, "
        "rather than substituting a zero that somebody would act on."
    ),
    checks={"database": database_ready},
)

app.include_router(router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
