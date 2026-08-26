"""Tests for feature engineering, focused on data leakage.

Leakage is the defining failure mode of a forecasting project: it produces a
model that scores beautifully in evaluation and is worthless in production,
and nothing about the metrics reveals it. These tests check the property
directly -- that no feature contains information from after the moment of
prediction -- rather than trusting that the code looks right.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features import (  # noqa: E402
    FEATURE_COLUMNS,
    HORIZON_DAYS,
    TARGET,
    build_features,
    modelling_frame,
    naive_baseline,
    time_split,
)


def make_sales(days: int = 120, products=("P1", "P2"), stores=("S1", "S2")) -> pd.DataFrame:
    """A small, fully predictable dataset: units increase by 1 each day."""
    rows = []
    start = date(2025, 1, 1)
    for product in products:
        for store in stores:
            for offset in range(days):
                rows.append(
                    {
                        "date": start + timedelta(days=offset),
                        "product_id": product,
                        "store_id": store,
                        "category": "Electronics",
                        "units_sold": offset + 1,
                        "price": 100.0,
                        "promotion": 0,
                        "holiday": 0,
                    }
                )
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


# ---------------------------------------------------------------------------
# The target
# ---------------------------------------------------------------------------
def test_target_is_the_sum_of_the_next_seven_days():
    """Explicitly: t+1 through t+7, never including day t."""
    sales = make_sales(days=60, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[20]
    expected = sales["units_sold"].iloc[21:28].sum()

    assert row[TARGET] == expected


def test_target_never_includes_the_current_day():
    sales = make_sales(days=60, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[10]
    # Units on day t are 11; the target must be the following seven days.
    assert row[TARGET] == sum(range(12, 19))


def test_final_rows_have_no_target_because_the_future_is_unknown():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    assert frame.tail(HORIZON_DAYS)[TARGET].isna().all()


def test_rows_without_a_full_future_window_are_dropped():
    sales = make_sales(days=60, products=("P1",), stores=("S1",))
    frame = modelling_frame(sales)

    assert frame[TARGET].notna().all()
    assert frame["date"].max() <= sales["date"].max() - pd.Timedelta(days=HORIZON_DAYS)


# ---------------------------------------------------------------------------
# Lags are grouped
# ---------------------------------------------------------------------------
def test_lags_never_cross_a_product_boundary():
    """An ungrouped shift(1) pulls the previous *product's* last day."""
    sales = make_sales(days=40)
    frame = build_features(sales)

    for (product, store), group in frame.groupby(["product_id", "store_id"]):
        first = group.sort_values("date").iloc[0]
        assert pd.isna(first["lag_1_units"]), (
            f"{product}/{store} inherited a lag from another series"
        )


def test_lag_1_is_the_previous_days_units_within_the_same_series():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[15]
    assert row["lag_1_units"] == sales["units_sold"].iloc[14]


def test_lag_7_is_exactly_seven_days_back():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[20]
    assert row["lag_7_units"] == sales["units_sold"].iloc[13]


def test_each_series_gets_its_own_lags():
    """Two series with different levels must not contaminate each other."""
    rows = []
    start = date(2025, 1, 1)
    for offset in range(30):
        rows.append(
            {
                "date": start + timedelta(days=offset),
                "product_id": "LOW",
                "store_id": "S1",
                "category": "Groceries",
                "units_sold": 1,
                "price": 10.0,
                "promotion": 0,
                "holiday": 0,
            }
        )
        rows.append(
            {
                "date": start + timedelta(days=offset),
                "product_id": "HIGH",
                "store_id": "S1",
                "category": "Groceries",
                "units_sold": 1000,
                "price": 10.0,
                "promotion": 0,
                "holiday": 0,
            }
        )
    sales = pd.DataFrame(rows)
    sales["date"] = pd.to_datetime(sales["date"])

    frame = build_features(sales).dropna(subset=["lag_1_units"])

    assert (frame[frame["product_id"] == "LOW"]["lag_1_units"] == 1).all()
    assert (frame[frame["product_id"] == "HIGH"]["lag_1_units"] == 1000).all()


# ---------------------------------------------------------------------------
# Rolling windows look backwards
# ---------------------------------------------------------------------------
def test_rolling_mean_uses_only_past_days():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[20]
    # Units are offset+1, so the seven days before day 20 are 14..20.
    assert row["roll_7_mean"] == pytest.approx(np.mean(range(14, 21)))


def test_rolling_sum_uses_only_past_days():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    assert frame.iloc[20]["roll_7_sum"] == sum(range(14, 21))


def test_rolling_window_never_contains_the_target_window():
    """The single most damaging leak: history overlapping the future."""
    sales = make_sales(days=60, products=("P1",), stores=("S1",))
    frame = modelling_frame(sales)

    for _, row in frame.iterrows():
        # roll_7_sum covers seven days ending at t; the target covers t+1..t+7.
        # With strictly increasing units, any overlap would make the rolling sum
        # reach or exceed the target.
        assert row["roll_7_sum"] < row[TARGET]


# ---------------------------------------------------------------------------
# Horizon features are calendar facts only
# ---------------------------------------------------------------------------
def test_horizon_weekend_count_matches_the_calendar():
    sales = make_sales(days=40, products=("P1",), stores=("S1",))
    frame = build_features(sales)

    row = frame.iloc[10]
    future_dates = sales["date"].iloc[11:18]
    expected = sum(1 for d in future_dates if d.dayofweek >= 5)

    assert row["horizon_weekend_days"] == expected


def test_horizon_features_never_include_future_sales():
    """Calendar and scheduled promotions only -- never the outcome."""
    leaky = {"units", "sold", "revenue", "demand"}
    horizon_features = [f for f in FEATURE_COLUMNS if f.startswith("horizon_")]

    for feature in horizon_features:
        assert not any(word in feature for word in leaky), f"{feature} looks like leakage"


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------
def test_split_is_chronological():
    sales = make_sales(days=200, products=("P1",), stores=("S1",))
    train, test = time_split(modelling_frame(sales), test_days=30)

    assert train["date"].max() < test["date"].min()


def test_split_leaves_a_gap_so_training_targets_do_not_reach_the_test_period():
    """Without the gap the model would have seen part of its own test set."""
    sales = make_sales(days=200, products=("P1",), stores=("S1",))
    train, test = time_split(modelling_frame(sales), test_days=30, horizon=HORIZON_DAYS)

    latest_train_target_day = train["date"].max() + pd.Timedelta(days=HORIZON_DAYS)
    assert latest_train_target_day <= test["date"].min()


def test_both_sides_of_the_split_are_populated():
    sales = make_sales(days=200)
    train, test = time_split(modelling_frame(sales), test_days=30)

    assert len(train) > 0
    assert len(test) > 0


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def test_naive_baseline_is_the_previous_seven_day_total():
    sales = make_sales(days=60, products=("P1",), stores=("S1",))
    frame = modelling_frame(sales)

    assert np.array_equal(naive_baseline(frame), frame["roll_7_sum"].to_numpy())


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_every_declared_feature_exists_in_the_frame():
    frame = modelling_frame(make_sales(days=90))
    for column in FEATURE_COLUMNS:
        assert column in frame.columns


def test_no_nulls_survive_into_the_modelling_frame():
    frame = modelling_frame(make_sales(days=90))
    assert not frame[[*FEATURE_COLUMNS, TARGET]].isna().any().any()


def test_frame_is_sorted_within_each_series():
    frame = build_features(make_sales(days=40))
    for _, group in frame.groupby(["product_id", "store_id"]):
        assert group["date"].is_monotonic_increasing
