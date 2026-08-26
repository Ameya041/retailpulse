"""Training and evaluation pipeline for the demand forecaster.

Run:

    python ml/train.py

Writes a versioned model artifact plus a metrics report. Every number in the
README comes from this script -- none are typed by hand.

## Evaluation

Two things make an evaluation trustworthy, and both are easy to skip:

**A time-based split.** Training on the future and testing on the past is
physically impossible in production and inflates every metric. The split is by
date, with a gap so no training row's target window reaches into the test
period.

**A baseline.** An MAE of 12 units means nothing on its own. Compared against
"next week will look like last week" -- what a shopkeeper would say without any
model at all -- it becomes a statement about whether the model earns its
existence. If the model cannot beat that, it should not ship.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

sys.path.insert(0, str(Path(__file__).resolve().parent))

from features import (  # noqa: E402
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    HORIZON_DAYS,
    TARGET,
    modelling_frame,
    naive_baseline,
    seasonal_baseline,
    time_split,
)

MODEL_VERSION = "v1"
ARTIFACT_DIR = Path("ml/artifacts")
DATA_PATH = Path("data/generated/sales.csv")


@dataclass
class Metrics:
    mae: float
    rmse: float
    r2: float
    mape: float

    @classmethod
    def compute(cls, actual: np.ndarray, predicted: np.ndarray) -> Metrics:
        actual = np.asarray(actual, dtype=float)
        predicted = np.asarray(predicted, dtype=float)
        # MAPE is undefined where actual is zero, which happens for slow-moving
        # products. Those rows are excluded from MAPE only -- silently treating
        # them as zero error would flatter the model.
        nonzero = actual != 0
        mape = (
            float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100)
            if nonzero.any()
            else float("nan")
        )
        return cls(
            mae=float(mean_absolute_error(actual, predicted)),
            rmse=float(np.sqrt(mean_squared_error(actual, predicted))),
            r2=float(r2_score(actual, predicted)),
            mape=mape,
        )


def build_pipeline(model_name: str) -> Pipeline:
    """Model plus preprocessing as one object.

    Bundling the encoder with the estimator means the exact transformation used
    at training time is what runs at inference. Fitting an encoder separately
    and re-creating it in the serving code is a reliable way to produce
    training/serving skew.
    """
    if model_name == "gradient_boosting":
        estimator = GradientBoostingRegressor(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=4,
            subsample=0.9,
            random_state=42,
        )
    elif model_name == "random_forest":
        estimator = RandomForestRegressor(
            n_estimators=300,
            max_depth=14,
            min_samples_leaf=3,
            n_jobs=-1,
            random_state=42,
        )
    else:  # pragma: no cover - guarded by argparse choices
        raise ValueError(f"Unknown model: {model_name}")

    numeric = [c for c in FEATURE_COLUMNS if c not in CATEGORICAL_FEATURES]
    preprocessor = ColumnTransformer(
        transformers=[
            # handle_unknown='ignore' matters: a store or category added after
            # training must not crash inference.
            ("categorical", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
            ("numeric", "passthrough", numeric),
        ]
    )
    return Pipeline([("preprocess", preprocessor), ("model", estimator)])


def train(
    sales: pd.DataFrame,
    *,
    model_name: str = "gradient_boosting",
    test_days: int = 60,
    horizon: int = HORIZON_DAYS,
) -> dict:
    frame = modelling_frame(sales, horizon=horizon)
    train_df, test_df = time_split(frame, test_days=test_days, horizon=horizon)

    if train_df.empty or test_df.empty:  # pragma: no cover - defensive
        raise ValueError("Not enough data to build a train/test split.")

    x_train, y_train = train_df[FEATURE_COLUMNS], train_df[TARGET]
    x_test, y_test = test_df[FEATURE_COLUMNS], test_df[TARGET]

    pipeline = build_pipeline(model_name)
    pipeline.fit(x_train, y_train)

    # Demand cannot be negative; a regressor can happily predict -3 units.
    predictions = np.clip(pipeline.predict(x_test), 0, None)

    model_metrics = Metrics.compute(y_test.to_numpy(), predictions)
    naive_metrics = Metrics.compute(y_test.to_numpy(), np.clip(naive_baseline(test_df), 0, None))
    seasonal_metrics = Metrics.compute(
        y_test.to_numpy(), np.clip(seasonal_baseline(test_df), 0, None)
    )

    improvement = (
        (naive_metrics.mae - model_metrics.mae) / naive_metrics.mae * 100
        if naive_metrics.mae
        else 0.0
    )

    return {
        "model_name": model_name,
        "model_version": MODEL_VERSION,
        "horizon_days": horizon,
        "trained_at": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "rows_total": int(len(frame)),
        "rows_train": int(len(train_df)),
        "rows_test": int(len(test_df)),
        "train_period": [str(train_df["date"].min().date()), str(train_df["date"].max().date())],
        "test_period": [str(test_df["date"].min().date()), str(test_df["date"].max().date())],
        "features": FEATURE_COLUMNS,
        "metrics": asdict(model_metrics),
        "baseline_naive_last_7_days": asdict(naive_metrics),
        "baseline_same_weekday": asdict(seasonal_metrics),
        "mae_improvement_over_naive_pct": round(improvement, 2),
        "beats_baseline": model_metrics.mae < naive_metrics.mae,
        "_pipeline": pipeline,
        "_test_frame": test_df,
        "_predictions": predictions,
    }


def feature_importances(pipeline: Pipeline) -> list[dict]:
    """Which features the model actually leans on.

    Worth printing: if a feature that should be irrelevant dominates, that is
    usually leakage rather than insight.
    """
    model = pipeline.named_steps["model"]
    if not hasattr(model, "feature_importances_"):  # pragma: no cover
        return []
    names = pipeline.named_steps["preprocess"].get_feature_names_out()
    pairs = sorted(
        zip(names, model.feature_importances_, strict=True),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [{"feature": str(n), "importance": round(float(v), 4)} for n, v in pairs[:15]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the demand forecasting model.")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument(
        "--model", default="gradient_boosting", choices=["gradient_boosting", "random_forest"]
    )
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--out", default=str(ARTIFACT_DIR))
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"No dataset at {data_path}. Run: python ml/generate_dataset.py", file=sys.stderr)
        return 1

    sales = pd.read_csv(data_path, parse_dates=["date"])
    print(f"Loaded {len(sales):,} rows from {data_path}")

    report = train(sales, model_name=args.model, test_days=args.test_days)

    pipeline = report.pop("_pipeline")
    report.pop("_test_frame")
    report.pop("_predictions")
    report["feature_importances"] = feature_importances(pipeline)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"demand_forecaster_{MODEL_VERSION}.joblib"
    metrics_path = out_dir / f"metrics_{MODEL_VERSION}.json"

    joblib.dump({"pipeline": pipeline, "metadata": report}, model_path)
    metrics_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    metrics = report["metrics"]
    naive = report["baseline_naive_last_7_days"]
    seasonal = report["baseline_same_weekday"]

    print()
    print(f"Model: {report['model_name']} ({report['model_version']})")
    print(f"  train {report['train_period'][0]} to {report['train_period'][1]}  ({report['rows_train']:,} rows)")
    print(f"  test  {report['test_period'][0]} to {report['test_period'][1]}  ({report['rows_test']:,} rows)")
    print()
    print(f"{'':<28}{'MAE':>10}{'RMSE':>10}{'R2':>10}")
    print(f"{'model':<28}{metrics['mae']:>10.3f}{metrics['rmse']:>10.3f}{metrics['r2']:>10.3f}")
    print(f"{'baseline: last 7 days':<28}{naive['mae']:>10.3f}{naive['rmse']:>10.3f}{naive['r2']:>10.3f}")
    print(f"{'baseline: same weekday':<28}{seasonal['mae']:>10.3f}{seasonal['rmse']:>10.3f}{seasonal['r2']:>10.3f}")
    print()
    print(f"MAE improvement over naive baseline: {report['mae_improvement_over_naive_pct']}%")
    print(f"Beats baseline: {report['beats_baseline']}")
    print()
    print("Top features:")
    for row in report["feature_importances"][:8]:
        print(f"  {row['importance']:>7.4f}  {row['feature']}")
    print()
    print(f"Saved model   -> {model_path}")
    print(f"Saved metrics -> {metrics_path}")

    # A model that loses to the baseline is a failure, and the exit code should
    # say so -- otherwise CI would happily ship it.
    return 0 if report["beats_baseline"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
