"""Forecast API and replenishment tests."""

from __future__ import annotations

import pytest

from app.forecaster import InsufficientHistoryError
from tests.conftest import KNOWN_PRODUCT, KNOWN_STORE


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------
def test_forecast_returns_one_entry_per_day(client, staff_headers):
    response = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "forecast_days": 7},
        headers=staff_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["forecast"]) == 7
    assert body["product_id"] == KNOWN_PRODUCT


def test_daily_figures_sum_to_the_reported_total(client, staff_headers):
    """The API must not contradict itself after rounding."""
    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE},
        headers=staff_headers,
    ).json()

    assert sum(day["predicted_units"] for day in body["forecast"]) == body["total_predicted_units"]


def test_forecast_dates_start_the_day_after_the_last_known_sale(client, staff_headers):
    from datetime import date, timedelta

    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE},
        headers=staff_headers,
    ).json()

    generated_from = date.fromisoformat(body["generated_from"])
    first_forecast = date.fromisoformat(body["forecast"][0]["date"])

    assert first_forecast == generated_from + timedelta(days=1)


def test_forecast_dates_are_consecutive(client, staff_headers):
    from datetime import date

    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "forecast_days": 14},
        headers=staff_headers,
    ).json()

    dates = [date.fromisoformat(d["date"]) for d in body["forecast"]]
    gaps = {(b - a).days for a, b in zip(dates[:-1], dates[1:], strict=True)}

    assert gaps == {1}


def test_predictions_are_never_negative(client, staff_headers):
    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE},
        headers=staff_headers,
    ).json()

    assert all(day["predicted_units"] >= 0 for day in body["forecast"])


def test_a_non_native_horizon_is_flagged_in_the_response(client, staff_headers):
    """A consumer must never be handed a scaled number as if it were native."""
    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "forecast_days": 14},
        headers=staff_headers,
    ).json()

    assert body["note"] is not None
    assert "7-day total" in body["note"]


def test_the_native_horizon_carries_no_caveat(client, staff_headers):
    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "forecast_days": 7},
        headers=staff_headers,
    ).json()

    assert body["note"] is None


def test_a_longer_horizon_forecasts_more_units(client, staff_headers):
    def total(days):
        return client.post(
            "/forecast",
            json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "forecast_days": days},
            headers=staff_headers,
        ).json()["total_predicted_units"]

    assert total(14) > total(7)


def test_model_version_is_reported(client, staff_headers):
    body = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE},
        headers=staff_headers,
    ).json()

    assert body["model_version"] == "v1"


def test_get_form_defaults_to_a_store_holding_the_product(client, staff_headers):
    response = client.get(f"/forecast/{KNOWN_PRODUCT}", headers=staff_headers)

    assert response.status_code == 200
    assert response.json()["store_id"] in ("BLR01", "MAA01")


def test_get_form_accepts_an_explicit_store(client, staff_headers):
    body = client.get(
        f"/forecast/{KNOWN_PRODUCT}?store_id=MAA01", headers=staff_headers
    ).json()

    assert body["store_id"] == "MAA01"


# ---------------------------------------------------------------------------
# Unknown series
# ---------------------------------------------------------------------------
def test_unknown_product_returns_404(client, staff_headers):
    assert client.get("/forecast/NO-SUCH-PRODUCT", headers=staff_headers).status_code == 404


def test_unknown_series_returns_400_with_a_reason(client, staff_headers):
    response = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": "NOWHERE"},
        headers=staff_headers,
    )

    assert response.status_code == 400
    assert "history" in response.json()["error"]["message"].lower()


def test_a_series_with_too_little_history_is_rejected(forecaster):
    """Better an explicit error than a confident forecast from three data points."""
    import pandas as pd

    short = forecaster.history[forecaster.history["product_id"] == KNOWN_PRODUCT].head(10)
    stub = type(forecaster)(forecaster.pipeline, forecaster.metadata, short)

    with pytest.raises(InsufficientHistoryError):
        stub.forecast(KNOWN_PRODUCT, KNOWN_STORE)

    assert isinstance(short, pd.DataFrame)


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------
def test_replenishment_recommends_ordering_when_stock_is_short(client, staff_headers):
    body = client.post(
        "/forecast/replenishment",
        json={
            "product_id": KNOWN_PRODUCT,
            "store_id": KNOWN_STORE,
            "current_stock": 0,
            "reorder_threshold": 10,
        },
        headers=staff_headers,
    ).json()

    assert body["recommended_order_quantity"] > 0
    assert body["urgency"] == "CRITICAL"


def test_replenishment_recommends_nothing_when_well_stocked(client, staff_headers):
    body = client.post(
        "/forecast/replenishment",
        json={
            "product_id": KNOWN_PRODUCT,
            "store_id": KNOWN_STORE,
            "current_stock": 100_000,
        },
        headers=staff_headers,
    ).json()

    assert body["recommended_order_quantity"] == 0
    assert body["urgency"] == "NONE"


def test_recommended_quantity_covers_forecast_plus_a_buffer(client, staff_headers):
    body = client.post(
        "/forecast/replenishment",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "current_stock": 0},
        headers=staff_headers,
    ).json()

    # 20% safety stock over the forecast horizon.
    assert body["recommended_order_quantity"] >= body["predicted_demand_7d"]


def test_existing_stock_is_deducted_from_the_recommendation(client, staff_headers):
    def order_for(stock):
        return client.post(
            "/forecast/replenishment",
            json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "current_stock": stock},
            headers=staff_headers,
        ).json()["recommended_order_quantity"]

    assert order_for(50) < order_for(0)


def test_days_of_cover_is_reported(client, staff_headers):
    body = client.post(
        "/forecast/replenishment",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "current_stock": 40},
        headers=staff_headers,
    ).json()

    assert body["days_of_cover"] is not None
    assert body["days_of_cover"] > 0


@pytest.mark.parametrize(
    ("stock", "threshold", "expected"),
    [(0, 5, "CRITICAL"), (5, 5, "CRITICAL"), (1_000_000, 0, "NONE")],
)
def test_urgency_reflects_the_stock_position(client, staff_headers, stock, threshold, expected):
    body = client.post(
        "/forecast/replenishment",
        json={
            "product_id": KNOWN_PRODUCT,
            "store_id": KNOWN_STORE,
            "current_stock": stock,
            "reorder_threshold": threshold,
        },
        headers=staff_headers,
    ).json()

    assert body["urgency"] == expected


def test_negative_stock_is_rejected(client, staff_headers):
    response = client.post(
        "/forecast/replenishment",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "current_stock": -5},
        headers=staff_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Model transparency
# ---------------------------------------------------------------------------
def test_model_info_reports_accuracy_against_a_baseline(client):
    body = client.get("/model/info").json()

    assert body["metrics"]["mae"] > 0
    assert body["baseline_naive_last_7_days"]["mae"] > 0
    assert body["beats_baseline"] is True
    assert body["mae_improvement_over_naive_pct"] > 0


def test_model_info_reports_the_training_window(client):
    body = client.get("/model/info").json()

    assert body["train_period"][1] < body["test_period"][0]
    assert body["rows_train"] > 0
    assert body["rows_test"] > 0


def test_model_info_lists_the_features_used(client):
    features = client.get("/model/info").json()["features"]

    assert "roll_7_mean" in features
    assert "promotion" in features
    # No feature may look like the answer.
    assert not any("units_next" in f for f in features)


def test_forecastable_products_are_listed(client, staff_headers):
    body = client.get("/forecast/products", headers=staff_headers).json()

    assert len(body) > 0
    assert all(entry["stores"] for entry in body)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def test_forecasting_requires_staff(client, customer_headers):
    response = client.post(
        "/forecast",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE},
        headers=customer_headers,
    )
    assert response.status_code == 403


def test_forecasting_requires_authentication(client):
    response = client.post(
        "/forecast", json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE}
    )
    assert response.status_code == 401


def test_replenishment_requires_staff(client, customer_headers):
    response = client.post(
        "/forecast/replenishment",
        json={"product_id": KNOWN_PRODUCT, "store_id": KNOWN_STORE, "current_stock": 0},
        headers=customer_headers,
    )
    assert response.status_code == 403


def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "ml-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/forecast" in paths
    assert "/model/info" in paths
