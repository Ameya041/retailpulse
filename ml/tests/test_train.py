"""Tests for the training pipeline.

The structural leakage tests live in test_features.py. These add the empirical
counterpart: train the real pipeline and check the results have the shape that
a non-leaking, genuinely-predictive model produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features import FEATURE_COLUMNS, TARGET, modelling_frame, time_split  # noqa: E402
from generate_dataset import generate  # noqa: E402
from train import Metrics, build_pipeline, feature_importances, train  # noqa: E402


@pytest.fixture(scope="module")
def sales() -> pd.DataFrame:
    """Nine months for two stores -- enough signal to learn, fast to train.

    Kept deliberately small: these tests run on every push, and training the
    full two-year dataset per test would make the suite unusable.
    """
    from datetime import date

    frame, _catalog = generate(date(2024, 1, 1), 270, seed=7)
    return frame[frame["store_id"].isin(["BLR01", "MAA01"])].reset_index(drop=True)


@pytest.fixture(scope="module")
def report(sales) -> dict:
    """Trained once and shared. Every test that needs a fitted model reuses
    this rather than retraining, which is the difference between a suite that
    runs in seconds and one that runs in minutes."""
    return train(sales, model_name="gradient_boosting", test_days=45)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def test_perfect_predictions_score_zero_error():
    actual = np.array([10.0, 20.0, 30.0])
    metrics = Metrics.compute(actual, actual)

    assert metrics.mae == 0.0
    assert metrics.rmse == 0.0
    assert metrics.r2 == 1.0


def test_rmse_punishes_large_errors_more_than_mae():
    actual = np.array([10.0, 10.0, 10.0, 10.0])
    predicted = np.array([10.0, 10.0, 10.0, 50.0])  # one big miss

    metrics = Metrics.compute(actual, predicted)

    assert metrics.rmse > metrics.mae


def test_mape_ignores_zero_actuals_rather_than_dividing_by_zero():
    actual = np.array([0.0, 10.0])
    predicted = np.array([5.0, 11.0])

    metrics = Metrics.compute(actual, predicted)

    assert np.isfinite(metrics.mape)
    assert metrics.mape == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# The trained model
# ---------------------------------------------------------------------------
def test_model_beats_the_naive_baseline(report):
    """Not beating 'next week looks like last week' means it should not ship."""
    assert report["beats_baseline"] is True
    assert report["metrics"]["mae"] < report["baseline_naive_last_7_days"]["mae"]


def test_improvement_over_baseline_is_meaningful(report):
    assert report["mae_improvement_over_naive_pct"] > 5.0


def test_metrics_are_finite_and_sane(report):
    metrics = report["metrics"]

    assert metrics["mae"] > 0, "zero error would mean the target leaked in"
    assert np.isfinite(metrics["rmse"])
    assert metrics["rmse"] >= metrics["mae"], "RMSE is never below MAE"
    assert 0.0 < metrics["r2"] <= 1.0


def test_train_and_test_periods_do_not_overlap(report):
    assert report["train_period"][1] < report["test_period"][0]


def test_test_set_is_not_trivially_small(report):
    assert report["rows_test"] > 500


def test_reported_features_match_the_declared_list(report):
    assert report["features"] == FEATURE_COLUMNS


# ---------------------------------------------------------------------------
# The empirical leakage check
# ---------------------------------------------------------------------------
def test_a_shuffled_target_destroys_all_skill(sales):
    """The decisive test for leakage.

    If any feature secretly contained the answer, the model would still score
    well after the targets are shuffled -- because the answer would still be
    sitting in the inputs. Shuffling breaks the real relationship, so a clean
    pipeline must collapse to no better than predicting the mean (R2 <= ~0).
    """
    frame = modelling_frame(sales)
    train_df, test_df = time_split(frame, test_days=45)

    rng = np.random.default_rng(0)
    shuffled = train_df.copy()
    shuffled[TARGET] = rng.permutation(shuffled[TARGET].to_numpy())

    pipeline = build_pipeline("gradient_boosting")
    pipeline.fit(shuffled[FEATURE_COLUMNS], shuffled[TARGET])
    predictions = np.clip(pipeline.predict(test_df[FEATURE_COLUMNS]), 0, None)

    r2 = Metrics.compute(test_df[TARGET].to_numpy(), predictions).r2

    assert r2 < 0.1, f"model retained skill on shuffled targets (R2={r2:.3f}) -- leakage"


def test_history_features_carry_the_signal_not_the_horizon_ones(report):
    """Sanity check on what the model leans on.

    Recent sales history should dominate. If a horizon feature dominated
    instead, that would suggest something future-facing is doing more work than
    it should.
    """
    importances = feature_importances(report["_pipeline"])
    top_feature = importances[0]["feature"]

    assert any(
        marker in top_feature for marker in ("roll_", "lag_")
    ), f"expected recent history to dominate, got {top_feature}"


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------
def test_predictions_are_never_negative(report):
    """A regressor will happily predict -3 units; demand cannot be negative."""
    assert (report["_predictions"] >= 0).all()


def test_predictions_are_in_a_plausible_range(report):
    predictions = report["_predictions"]
    actual = report["_test_frame"][TARGET].to_numpy()

    assert predictions.max() < actual.max() * 3, "predictions wildly overshoot"
    assert predictions.mean() == pytest.approx(actual.mean(), rel=0.35)


def test_random_forest_also_beats_the_baseline(sales):
    """The choice of estimator should not be load-bearing."""
    rf_report = train(sales, model_name="random_forest", test_days=45)
    assert rf_report["beats_baseline"] is True


def test_the_model_loses_to_the_baseline_on_an_unseen_season():
    """Documents a real limitation, measured rather than assumed.

    The generated data has a festive lift in October and November. A model
    trained on less than a full year has never observed that season, and it
    cannot extrapolate a pattern it has not seen. Across a seasonal regime
    change it therefore performs *worse* than the naive baseline, which simply
    carries the recent level forward and so adapts automatically.

    Measured across training windows (2 stores, 45-day test):

        180 days, test May-Jun    +10.8%   (no regime change)
        270 days, test Aug-Sep    +22.5%   (no regime change)
        300 days, test Sep-Oct    -33.4%   (crosses INTO the festive lift)
        365 days, test Nov-Dec    -14.9%   (crosses OUT of it)
        730 days, test Nov-Dec    +19.9%   (same months seen last year)

    This is why the shipped model is trained on two years. The test pins the
    behaviour so that a future change which appears to "fix" short-history
    performance gets examined rather than trusted.
    """
    from datetime import date

    frame, _catalog = generate(date(2024, 1, 1), 300, seed=11)
    frame = frame[frame["store_id"].isin(["BLR01", "MAA01"])].reset_index(drop=True)

    report = train(frame, model_name="gradient_boosting", test_days=45)

    # The test window is Sep-Oct, the model has never seen an October.
    assert report["test_period"][0].startswith("2024-09")
    assert report["beats_baseline"] is False, (
        "short-history model unexpectedly beat the baseline across an unseen "
        "season -- verify the seasonality is still present in the generator"
    )


def test_two_years_of_history_beats_the_baseline_on_the_same_season():
    """The counterpart: with the season observed once, the model wins."""
    from datetime import date

    frame, _catalog = generate(date(2024, 1, 1), 730, seed=11)
    frame = frame[frame["store_id"].isin(["BLR01", "MAA01"])].reset_index(drop=True)

    report = train(frame, model_name="gradient_boosting", test_days=45)

    # Same calendar months as the failing case above, one year later.
    assert report["test_period"][0].startswith("2025-11")
    assert report["beats_baseline"] is True
    assert report["mae_improvement_over_naive_pct"] > 10


def test_unknown_model_name_is_rejected():
    with pytest.raises(ValueError):
        build_pipeline("magic_crystal_ball")


def test_an_unseen_category_does_not_crash_inference(report):
    """A store or category added after training must not break the API.

    This is what `handle_unknown='ignore'` on the encoder buys.
    """
    row = report["_test_frame"].head(1).copy()
    row["category"] = "Category That Did Not Exist At Training Time"
    row["store_id"] = "NEW01"

    prediction = report["_pipeline"].predict(row[FEATURE_COLUMNS])

    assert np.isfinite(prediction).all()
