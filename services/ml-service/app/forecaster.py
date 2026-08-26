"""Inference for the demand forecaster.

## What the model gives us, and what the API promises

The model predicts a **7-day total**. The API is asked for a per-day forecast,
so the total is allocated across days using the weekday pattern learned from
that series' own history.

That is stated plainly rather than dressed up: the daily numbers are a
seasonal *allocation* of one prediction, not seven independent predictions.
Presenting them as independent would imply a precision the model does not have.
The 7-day total is the number to trust, and it is the number replenishment
actually uses.

## Where the history comes from

Feature construction needs recent sales for the series being forecast. This
service loads a history snapshot produced by the data pipeline and keeps it in
memory -- at this size (a few hundred thousand rows) that is far cheaper than
querying per request.

In production this would be a feature store, or a scheduled job materialising
features into a serving table. The interface here is deliberately narrow
(`history_for`) so swapping the source touches one method.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# The feature code is shared with training on purpose: reimplementing it here
# is the classic route to training/serving skew, where the model is fed
# subtly different inputs than it learned from.
ML_ROOT = Path(__file__).resolve().parents[3] / "ml"
if str(ML_ROOT) not in sys.path:
    sys.path.insert(0, str(ML_ROOT))

from features import FEATURE_COLUMNS, HORIZON_DAYS, build_features  # noqa: E402

logger = logging.getLogger("ml-service")

# Enough trailing history to fill the longest rolling window (28 days) with
# room to spare.
MIN_HISTORY_DAYS = 45


class ModelNotLoadedError(RuntimeError):
    """Raised when a forecast is requested before an artifact is available."""


class InsufficientHistoryError(ValueError):
    """Not enough trailing sales to build features for this series."""


@dataclass(frozen=True)
class DailyForecast:
    forecast_date: date
    predicted_units: int


@dataclass(frozen=True)
class ForecastResult:
    product_id: str
    store_id: str
    forecast: list[DailyForecast]
    total_predicted_units: int
    model_version: str
    horizon_days: int
    history_days_used: int
    generated_from: date


class DemandForecaster:
    def __init__(self, pipeline, metadata: dict, history: pd.DataFrame) -> None:
        self.pipeline = pipeline
        self.metadata = metadata
        self.history = history
        self.model_version = metadata.get("model_version", "unknown")

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, model_path: Path, history_path: Path) -> DemandForecaster:
        import joblib

        if not model_path.exists():
            raise ModelNotLoadedError(
                f"No model artifact at {model_path}. Run: python ml/train.py"
            )
        if not history_path.exists():
            raise ModelNotLoadedError(
                f"No history at {history_path}. Run: python ml/generate_dataset.py"
            )

        bundle = joblib.load(model_path)
        history = pd.read_csv(history_path, parse_dates=["date"])
        history = history.sort_values(["product_id", "store_id", "date"])

        logger.info(
            "forecaster loaded",
            extra={
                "model_version": bundle["metadata"].get("model_version"),
                "history_rows": len(history),
            },
        )
        return cls(bundle["pipeline"], bundle["metadata"], history)

    # ------------------------------------------------------------------
    # History access -- the seam a feature store would replace
    # ------------------------------------------------------------------
    def history_for(self, product_id: str, store_id: str) -> pd.DataFrame:
        return self.history[
            (self.history["product_id"] == product_id)
            & (self.history["store_id"] == store_id)
        ]

    def known_products(self) -> list[str]:
        return sorted(self.history["product_id"].unique().tolist())

    def known_stores(self) -> list[str]:
        return sorted(self.history["store_id"].unique().tolist())

    def stores_for(self, product_id: str) -> list[str]:
        rows = self.history[self.history["product_id"] == product_id]
        return sorted(rows["store_id"].unique().tolist())

    # ------------------------------------------------------------------
    # Forecasting
    # ------------------------------------------------------------------
    def _weekday_profile(self, series: pd.DataFrame) -> dict[int, float]:
        """Relative demand per weekday, from this series' own history.

        Falls back to a flat profile when a weekday has never been observed,
        rather than dividing by zero.
        """
        recent = series.tail(120)
        overall = recent["units_sold"].mean()
        if not overall:
            return dict.fromkeys(range(7), 1.0)

        by_day = recent.groupby(recent["date"].dt.dayofweek)["units_sold"].mean()
        return {day: float(by_day.get(day, overall) / overall) for day in range(7)}

    def forecast(
        self, product_id: str, store_id: str, forecast_days: int = HORIZON_DAYS
    ) -> ForecastResult:
        series = self.history_for(product_id, store_id)

        if series.empty:
            raise InsufficientHistoryError(
                f"No sales history for product {product_id} at store {store_id}."
            )
        if len(series) < MIN_HISTORY_DAYS:
            raise InsufficientHistoryError(
                f"Only {len(series)} days of history for {product_id} at {store_id}; "
                f"at least {MIN_HISTORY_DAYS} are needed to build features."
            )

        # build_features needs the future columns to exist; the horizon values
        # for the final row are unknown, so they are filled from the calendar
        # and from recent promotion frequency below.
        prepared = self._prepare_inference_row(series)

        predicted_total = float(np.clip(self.pipeline.predict(prepared)[0], 0, None))

        last_date = series["date"].max().date()
        profile = self._weekday_profile(series)
        forecast_dates = [last_date + timedelta(days=n) for n in range(1, forecast_days + 1)]

        # Allocate the total across days in proportion to weekday demand, then
        # round. Rounding is corrected on the final day so the daily figures
        # always sum to the total the model actually predicted -- otherwise the
        # API would contradict itself.
        weights = np.array([profile.get(d.weekday(), 1.0) for d in forecast_dates], dtype=float)
        weights = weights / weights.sum() if weights.sum() else np.full(forecast_days, 1 / forecast_days)

        # The model predicts a 7-day total; a longer request is scaled pro rata
        # and flagged in the response.
        scaled_total = predicted_total * (forecast_days / HORIZON_DAYS)

        raw = weights * scaled_total
        allocated = [int(round(value)) for value in raw]
        drift = int(round(scaled_total)) - sum(allocated)
        if allocated:
            allocated[-1] = max(0, allocated[-1] + drift)

        return ForecastResult(
            product_id=product_id,
            store_id=store_id,
            forecast=[
                DailyForecast(forecast_date=d, predicted_units=max(0, units))
                for d, units in zip(forecast_dates, allocated, strict=True)
            ],
            total_predicted_units=sum(max(0, u) for u in allocated),
            model_version=self.model_version,
            horizon_days=forecast_days,
            history_days_used=len(series),
            generated_from=last_date,
        )

    def _prepare_inference_row(self, series: pd.DataFrame) -> pd.DataFrame:
        """Build the single feature row for the most recent day.

        Always built at the model's native 7-day horizon regardless of what the
        caller asked for -- the features must match what the model was trained
        on. A longer requested horizon is scaled afterwards and flagged in the
        response.

        The horizon features describe days that have not happened. Calendar
        facts (weekends, holidays) are computed directly; promotions are
        estimated from this series' recent rate, because a real deployment
        would read the scheduled promotion calendar and this service has no
        access to one.
        """
        frame = build_features(series, horizon=HORIZON_DAYS)
        row = frame.tail(1).copy()

        last_date = series["date"].max().date()
        future = [last_date + timedelta(days=n) for n in range(1, HORIZON_DAYS + 1)]

        row["horizon_weekend_days"] = float(sum(1 for d in future if d.weekday() >= 5))
        # No holiday calendar available at inference; recent frequency is the
        # honest estimate rather than assuming zero.
        row["horizon_holiday_days"] = float(series.tail(90)["holiday"].mean() * HORIZON_DAYS)
        row["horizon_promo_days"] = float(series.tail(90)["promotion"].mean() * HORIZON_DAYS)
        row["horizon_mean_price"] = float(series.tail(14)["price"].mean())

        missing = row[FEATURE_COLUMNS].isna().any()
        if missing.any():  # pragma: no cover - guarded by the history check
            raise InsufficientHistoryError(
                f"Cannot build features; missing values for {list(missing[missing].index)}."
            )

        return row[FEATURE_COLUMNS]

    # ------------------------------------------------------------------
    # Replenishment
    # ------------------------------------------------------------------
    def recommend_replenishment(
        self, product_id: str, store_id: str, current_stock: int, reorder_threshold: int = 0
    ) -> dict:
        """Turn a forecast into a stock decision.

        Recommends enough to cover forecast demand plus a safety buffer, minus
        what is already on the shelf. The buffer exists because the cost of a
        stockout (a lost sale and a lost customer) is usually higher than the
        cost of holding a few extra units.
        """
        result = self.forecast(product_id, store_id)
        predicted = result.total_predicted_units

        # 20% safety stock over the forecast horizon.
        target = int(round(predicted * 1.2)) + reorder_threshold
        recommended = max(0, target - current_stock)

        if current_stock <= reorder_threshold:
            urgency = "CRITICAL"
        elif current_stock < predicted:
            urgency = "HIGH"
        elif current_stock < target:
            urgency = "MEDIUM"
        else:
            urgency = "NONE"

        return {
            "product_id": product_id,
            "store_id": store_id,
            "current_stock": current_stock,
            "predicted_demand_7d": predicted,
            "reorder_threshold": reorder_threshold,
            "target_stock": target,
            "recommended_order_quantity": recommended,
            "urgency": urgency,
            "days_of_cover": (
                round(current_stock / (predicted / HORIZON_DAYS), 1) if predicted else None
            ),
            "model_version": self.model_version,
        }
