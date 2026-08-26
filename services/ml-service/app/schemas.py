"""ML service API contracts."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field


class ForecastRequest(BaseModel):
    product_id: Annotated[str, Field(min_length=1, max_length=64)]
    store_id: Annotated[str, Field(min_length=1, max_length=16)]
    forecast_days: Annotated[int, Field(ge=1, le=28)] = 7


class DailyForecastRead(BaseModel):
    date: date
    predicted_units: int


class ForecastResponse(BaseModel):
    product_id: str
    store_id: str
    forecast: list[DailyForecastRead]
    total_predicted_units: int
    model_version: str
    horizon_days: int
    history_days_used: int
    generated_from: date
    # Set when the request asked for a horizon the model was not trained on, so
    # a consumer is never silently handed a scaled-up number as if it were a
    # native prediction.
    note: str | None = None


class ReplenishmentRequest(BaseModel):
    product_id: Annotated[str, Field(min_length=1, max_length=64)]
    store_id: Annotated[str, Field(min_length=1, max_length=16)]
    current_stock: Annotated[int, Field(ge=0)]
    reorder_threshold: Annotated[int, Field(ge=0)] = 0


class ReplenishmentResponse(BaseModel):
    product_id: str
    store_id: str
    current_stock: int
    predicted_demand_7d: int
    reorder_threshold: int
    target_stock: int
    recommended_order_quantity: int
    urgency: str
    days_of_cover: float | None
    model_version: str


class ModelMetrics(BaseModel):
    mae: float
    rmse: float
    r2: float
    mape: float


class ModelInfo(BaseModel):
    """Everything needed to judge whether the model should be trusted."""

    model_name: str
    model_version: str
    horizon_days: int
    trained_at: str
    rows_train: int
    rows_test: int
    train_period: list[str]
    test_period: list[str]
    features: list[str]
    metrics: ModelMetrics
    baseline_naive_last_7_days: ModelMetrics
    mae_improvement_over_naive_pct: float
    beats_baseline: bool


class SeriesRead(BaseModel):
    product_id: str
    stores: list[str]
