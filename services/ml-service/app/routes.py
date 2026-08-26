"""Forecast API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import get_forecaster, require_roles
from app.forecaster import DemandForecaster, InsufficientHistoryError
from app.schemas import (
    DailyForecastRead,
    ForecastRequest,
    ForecastResponse,
    ModelInfo,
    ReplenishmentRequest,
    ReplenishmentResponse,
    SeriesRead,
)
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/forecast", tags=["forecast"])
model_router = APIRouter(prefix="/model", tags=["model"])

ForecasterDep = Annotated[DemandForecaster, Depends(get_forecaster)]
StaffDep = Annotated[
    TokenPayload, Depends(require_roles(Role.ADMIN, Role.WAREHOUSE_OPERATOR))
]

AUTH_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Requires ADMIN or WAREHOUSE_OPERATOR."},
}

HORIZON_NOTE = (
    "The model predicts a 7-day total; this horizon was scaled pro rata. "
    "Daily figures are a weekday-seasonal allocation of that total, not "
    "independent per-day predictions."
)


def _to_response(result, forecast_days: int) -> ForecastResponse:
    return ForecastResponse(
        product_id=result.product_id,
        store_id=result.store_id,
        forecast=[
            DailyForecastRead(date=d.forecast_date, predicted_units=d.predicted_units)
            for d in result.forecast
        ],
        total_predicted_units=result.total_predicted_units,
        model_version=result.model_version,
        horizon_days=result.horizon_days,
        history_days_used=result.history_days_used,
        generated_from=result.generated_from,
        note=HORIZON_NOTE if forecast_days != 7 else None,
    )


@router.post(
    "",
    response_model=ForecastResponse,
    summary="Forecast demand for a product at a store",
    responses={
        **AUTH_ERRORS,
        400: {"description": "Not enough sales history for this series."},
        404: {"description": "Unknown product or store."},
    },
)
def create_forecast(
    payload: ForecastRequest, forecaster: ForecasterDep, _: StaffDep
) -> ForecastResponse:
    """Predict demand over the requested horizon.

    The model predicts a **7-day total**. Daily figures are that total
    allocated across days using the series' own weekday pattern -- an
    allocation, not seven independent predictions. The total is the number to
    plan against.
    """
    try:
        result = forecaster.forecast(
            payload.product_id, payload.store_id, payload.forecast_days
        )
    except InsufficientHistoryError as exc:
        raise ValidationError(str(exc), details={"product_id": payload.product_id}) from exc

    return _to_response(result, payload.forecast_days)


@router.get(
    "/products",
    response_model=list[SeriesRead],
    summary="Products the model can forecast",
    responses=AUTH_ERRORS,
)
def forecastable_products(forecaster: ForecasterDep, _: StaffDep) -> list[SeriesRead]:
    """Every series with enough history to produce a forecast."""
    return [
        SeriesRead(product_id=product_id, stores=forecaster.stores_for(product_id))
        for product_id in forecaster.known_products()
    ]


@router.post(
    "/replenishment",
    response_model=ReplenishmentResponse,
    summary="Recommend a reorder quantity",
    responses={**AUTH_ERRORS, 400: {"description": "Not enough sales history."}},
)
def replenishment(
    payload: ReplenishmentRequest, forecaster: ForecasterDep, _: StaffDep
) -> ReplenishmentResponse:
    """Turn a forecast into a stock decision.

    Recommends enough to cover predicted demand plus a 20% safety buffer, less
    what is already on the shelf. The buffer is deliberate: a stockout costs a
    sale and sometimes a customer, which is usually dearer than holding a few
    extra units.
    """
    try:
        recommendation = forecaster.recommend_replenishment(
            payload.product_id,
            payload.store_id,
            payload.current_stock,
            payload.reorder_threshold,
        )
    except InsufficientHistoryError as exc:
        raise ValidationError(str(exc), details={"product_id": payload.product_id}) from exc

    return ReplenishmentResponse(**recommendation)


@router.get(
    "/{product_id}",
    response_model=ForecastResponse,
    summary="Forecast a product at a store",
    responses={
        **AUTH_ERRORS,
        400: {"description": "Not enough sales history."},
        404: {"description": "Unknown product."},
    },
)
def get_forecast(
    product_id: str,
    forecaster: ForecasterDep,
    _: StaffDep,
    store_id: Annotated[str | None, Query(description="Defaults to the first store holding it.")] = None,
    forecast_days: Annotated[int, Query(ge=1, le=28)] = 7,
) -> ForecastResponse:
    """Convenience GET form of the forecast endpoint."""
    stores = forecaster.stores_for(product_id)
    if not stores:
        raise NotFoundError(
            f"No sales history for product {product_id}.",
            details={"product_id": product_id},
        )

    resolved_store = store_id or stores[0]
    try:
        result = forecaster.forecast(product_id, resolved_store, forecast_days)
    except InsufficientHistoryError as exc:
        raise ValidationError(str(exc), details={"product_id": product_id}) from exc

    return _to_response(result, forecast_days)


@model_router.get(
    "/info",
    response_model=ModelInfo,
    summary="Model version, training window and accuracy",
)
def model_info(forecaster: ForecasterDep) -> ModelInfo:
    """Everything needed to judge whether to trust this model.

    Public on purpose: accuracy against a documented baseline is not a secret,
    and hiding it invites the numbers to go unexamined.
    """
    return ModelInfo(**{k: v for k, v in forecaster.metadata.items() if k in ModelInfo.model_fields})
