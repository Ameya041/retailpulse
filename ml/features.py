"""Feature engineering for demand forecasting.

## What the model predicts

**The total units sold over the next 7 days**, given only what is known today.

That framing is chosen deliberately over "predict tomorrow, then feed the
prediction back in to predict the day after". Recursive forecasting compounds
its own error across the horizon and requires inventing future values for every
lag feature. A *direct* multi-horizon target has neither problem, and it
answers the question replenishment actually asks: how much stock do I need for
the coming week?

## Leakage: the failure that makes a model look brilliant and be useless

Every feature here must be computable **on day t** without seeing day t+1
onwards. Two traps:

1. **Lag features must be grouped.** A `shift(1)` over the whole frame pulls the
   previous row -- which, when rows are ordered by product then date, is a
   *different product's* last day at every boundary. Every lag is computed
   within a `(product_id, store_id)` group.

2. **Rolling windows must end at day t.** A centred or forward-looking window
   contains the very days being predicted. A model fed those scores almost
   perfectly in testing and fails completely in production, because the future
   is not available at prediction time.

There is one class of genuinely-future information included on purpose:
**calendar facts and scheduled promotions over the horizon**. Weekends and
holidays are known years ahead, and retailers plan promotions weeks ahead, so a
forecaster in production would have them. Excluding them would understate the
model rather than make it more honest. They are marked as ``horizon`` features
below so the choice is visible rather than buried.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZON_DAYS = 7

GROUP_KEYS = ["product_id", "store_id"]

#: Features computable from history up to and including day t.
HISTORY_FEATURES = [
    "lag_1_units",
    "lag_7_units",
    "roll_7_mean",
    "roll_7_sum",
    "roll_28_mean",
    "roll_7_std",
    "trailing_7_price_mean",
]

#: Facts about day t itself.
TODAY_FEATURES = [
    "day_of_week",
    "month",
    "is_weekend",
    "price",
    "promotion",
    "holiday",
]

#: Genuinely known in advance -- calendar, plus promotions the business has
#: already scheduled. Documented rather than hidden; see the module docstring.
HORIZON_FEATURES = [
    "horizon_weekend_days",
    "horizon_holiday_days",
    "horizon_promo_days",
    "horizon_mean_price",
]

CATEGORICAL_FEATURES = ["store_id", "category"]

FEATURE_COLUMNS = (
    HISTORY_FEATURES + TODAY_FEATURES + HORIZON_FEATURES + CATEGORICAL_FEATURES
)

TARGET = "units_next_7d"


def _reverse_rolling_sum(group: pd.Series, window: int) -> pd.Series:
    """Sum of the *next* `window` values, excluding the current row.

    Used only to build the target. Reversing, rolling, then reversing back is
    the clearest way to express a forward window with pandas.
    """
    return group.shift(-1)[::-1].rolling(window, min_periods=window).sum()[::-1]


def build_features(sales: pd.DataFrame, *, horizon: int = HORIZON_DAYS) -> pd.DataFrame:
    """Turn raw daily sales into a supervised learning frame.

    Input columns: date, product_id, store_id, category, units_sold, price,
    promotion, holiday.
    """
    frame = sales.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values([*GROUP_KEYS, "date"]).reset_index(drop=True)

    grouped = frame.groupby(GROUP_KEYS, sort=False, observed=True)

    # ---------------------------------------------------------------
    # History features -- all strictly backward-looking.
    # ---------------------------------------------------------------
    frame["lag_1_units"] = grouped["units_sold"].shift(1)
    frame["lag_7_units"] = grouped["units_sold"].shift(7)

    # `.rolling(7)` on the raw column would include day t. That is fine here --
    # the target starts at t+1 -- but the shift(1) is kept anyway so these
    # features stay valid if the horizon is ever changed to include day t.
    shifted_units = grouped["units_sold"].shift(1)
    rolled = shifted_units.groupby([frame[k] for k in GROUP_KEYS], sort=False, observed=True)
    frame["roll_7_mean"] = rolled.transform(lambda s: s.rolling(7, min_periods=3).mean())
    frame["roll_7_sum"] = rolled.transform(lambda s: s.rolling(7, min_periods=3).sum())
    frame["roll_7_std"] = rolled.transform(lambda s: s.rolling(7, min_periods=3).std())
    frame["roll_28_mean"] = rolled.transform(lambda s: s.rolling(28, min_periods=7).mean())

    shifted_price = grouped["price"].shift(1)
    frame["trailing_7_price_mean"] = shifted_price.groupby(
        [frame[k] for k in GROUP_KEYS], sort=False, observed=True
    ).transform(lambda s: s.rolling(7, min_periods=3).mean())

    # ---------------------------------------------------------------
    # Facts about day t.
    # ---------------------------------------------------------------
    frame["day_of_week"] = frame["date"].dt.dayofweek
    frame["month"] = frame["date"].dt.month
    frame["is_weekend"] = (frame["day_of_week"] >= 5).astype(int)

    # ---------------------------------------------------------------
    # Horizon features -- known in advance, see the module docstring.
    # ---------------------------------------------------------------
    frame["horizon_weekend_days"] = _forward_sum(frame, "is_weekend", horizon)
    frame["horizon_holiday_days"] = _forward_sum(frame, "holiday", horizon)
    frame["horizon_promo_days"] = _forward_sum(frame, "promotion", horizon)
    frame["horizon_mean_price"] = _forward_mean(frame, "price", horizon)

    # ---------------------------------------------------------------
    # Target.
    # ---------------------------------------------------------------
    frame[TARGET] = (
        frame.groupby(GROUP_KEYS, sort=False, observed=True)["units_sold"]
        .transform(lambda s: _reverse_rolling_sum(s, horizon))
    )

    return frame


def _forward_sum(frame: pd.DataFrame, column: str, horizon: int) -> pd.Series:
    return frame.groupby(GROUP_KEYS, sort=False, observed=True)[column].transform(
        lambda s: _reverse_rolling_sum(s, horizon)
    )


def _forward_mean(frame: pd.DataFrame, column: str, horizon: int) -> pd.Series:
    return frame.groupby(GROUP_KEYS, sort=False, observed=True)[column].transform(
        lambda s: s.shift(-1)[::-1].rolling(horizon, min_periods=horizon).mean()[::-1]
    )


def modelling_frame(sales: pd.DataFrame, *, horizon: int = HORIZON_DAYS) -> pd.DataFrame:
    """Features plus target, with unusable rows dropped.

    Rows are dropped at both ends for real reasons: the first few days of each
    series have no history to lag, and the last `horizon` days have no future
    to score against. Imputing either would be inventing data.
    """
    frame = build_features(sales, horizon=horizon)
    required = [*FEATURE_COLUMNS, TARGET, "date"]
    return frame.dropna(subset=required).reset_index(drop=True)


def time_split(
    frame: pd.DataFrame, *, test_days: int = 60, horizon: int = HORIZON_DAYS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split by time, never at random.

    A random split lets the model train on next week and be tested on last
    week, which is impossible in production and inflates every metric.

    A gap of `horizon` days is left between train and test. Without it, a
    training row from the final training day has a target window that overlaps
    the test period -- the model would have seen part of its own test set.
    """
    cutoff = frame["date"].max() - pd.Timedelta(days=test_days)
    gap_start = cutoff - pd.Timedelta(days=horizon)

    train = frame[frame["date"] <= gap_start]
    test = frame[frame["date"] > cutoff]
    return train.reset_index(drop=True), test.reset_index(drop=True)


def naive_baseline(frame: pd.DataFrame) -> np.ndarray:
    """The forecast to beat: 'the next 7 days will look like the last 7'.

    Any model that cannot beat this is not worth deploying, and quoting an MAE
    without this comparison says nothing about whether the model is any good.
    """
    return frame["roll_7_sum"].to_numpy()


def seasonal_baseline(frame: pd.DataFrame) -> np.ndarray:
    """A slightly stronger baseline: last week's same-weekday rate, scaled up."""
    return (frame["lag_7_units"] * HORIZON_DAYS).to_numpy()
