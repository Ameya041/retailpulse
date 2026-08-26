"""ML service entrypoint."""

from __future__ import annotations

from app.config import get_settings
from app.deps import model_ready
from app.routes import model_router, router
from retailpulse_common.app import create_service_app

settings = get_settings()

app = create_service_app(
    settings=settings,
    title="RetailPulse ML Service",
    description=(
        "Demand forecasting for replenishment.\n\n"
        "**What it predicts.** The total units a product will sell at a store over "
        "the next 7 days. Daily figures in the response are that total allocated "
        "across days using the series' own weekday pattern -- an allocation, not "
        "seven independent predictions. Saying so matters: presenting them as "
        "independent would imply precision the model does not have.\n\n"
        "**Why direct rather than recursive.** Predicting tomorrow and feeding that "
        "prediction back to predict the next day compounds error across the horizon "
        "and requires inventing future values for every lag feature. A direct "
        "multi-horizon target avoids both.\n\n"
        "**How good is it.** `GET /model/info` reports MAE and RMSE on a held-out "
        "time period, next to a naive baseline ('next week looks like last week'). "
        "An error figure without a baseline says nothing about whether a model earns "
        "its existence."
    ),
    checks={"model": model_ready},
)

app.include_router(router)
app.include_router(model_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
