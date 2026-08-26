"""Generate a synthetic retail sales history.

Real sales data is not available, so it is simulated -- but simulated with the
structure a forecasting model actually has to learn, otherwise the exercise is
meaningless. A dataset of uniform random numbers would let any model score well
by predicting the mean, and the reported MAE would say nothing.

The generator therefore builds in the effects that make retail demand
forecastable:

* **Weekly seasonality** -- weekends are busier than midweek.
* **Annual seasonality** -- a slow sine over the year, plus a festive lift
  around Diwali/Christmas.
* **Trend** -- gentle growth over the period.
* **Promotions** -- a large, price-driven demand spike.
* **Holidays** -- a smaller lift, independent of price.
* **Price elasticity** -- demand responds to price, with different categories
  responding differently (groceries are inelastic, electronics are not).
* **Store size** -- a multiplier per location.
* **Noise** -- Poisson, because unit sales are counts, not continuous values.

Because the effects are known, the model's error can be judged against a
theoretical floor: no model can beat the Poisson noise, so an MAE close to
sqrt(mean) is close to optimal rather than merely "small".

Deterministic given a seed, so every metric in the README is reproducible.
"""

from __future__ import annotations

import argparse
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_SEED = 20260826

# Store code -> (city, size multiplier). Bangalore is the flagship.
STORES: dict[str, tuple[str, float]] = {
    "BLR01": ("Bangalore", 1.45),
    "MAA01": ("Chennai", 1.10),
    "HYD01": ("Hyderabad", 1.00),
    "BOM01": ("Mumbai", 1.30),
    "DEL01": ("Delhi", 1.20),
}

# Category -> (base daily demand, price elasticity, base price).
# Elasticity is negative: higher price, lower demand. Groceries barely react;
# electronics react strongly.
CATEGORIES: dict[str, tuple[float, float, float]] = {
    "Groceries": (42.0, -0.35, 180.0),
    "Electronics": (7.0, -1.60, 24000.0),
    "Home Appliances": (5.0, -1.20, 9500.0),
    "Fashion": (14.0, -0.95, 2200.0),
    "Sports and Outdoors": (9.0, -0.80, 1900.0),
}

PRODUCTS_PER_CATEGORY = 6

# Indian festive season plus a couple of fixed holidays. Dates are approximate
# on purpose -- the model learns "holiday" as a flag, not the calendar.
HOLIDAY_MONTH_DAYS = {
    (1, 26),   # Republic Day
    (8, 15),   # Independence Day
    (10, 24),  # Diwali (approx)
    (12, 25),  # Christmas
}
FESTIVE_MONTHS = {10, 11}


def _is_holiday(day: date) -> bool:
    return (day.month, day.day) in HOLIDAY_MONTH_DAYS


def build_catalog(rng: np.random.Generator) -> pd.DataFrame:
    """One row per product, with a stable base price and category."""
    rows = []
    for category, (base_demand, elasticity, base_price) in CATEGORIES.items():
        # First three letters of the category. Using initials instead gave
        # one-character codes for single-word categories ("E-0001"), which are
        # unreadable in a warehouse and collide as categories are added.
        prefix = "".join(ch for ch in category if ch.isalpha())[:3].upper()
        for index in range(1, PRODUCTS_PER_CATEGORY + 1):
            # Spread prices around the category's midpoint so elasticity has
            # something to bite on.
            price_factor = float(rng.uniform(0.55, 1.75))
            rows.append(
                {
                    "product_id": f"{prefix}-{index:04d}",
                    "category": category,
                    "base_price": round(base_price * price_factor, 2),
                    "base_demand": base_demand * float(rng.uniform(0.6, 1.5)),
                    "elasticity": elasticity,
                }
            )
    return pd.DataFrame(rows)


def generate(
    start: date, days: int, seed: int = DEFAULT_SEED
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (sales, catalog).

    Sales has one row per (date, product, store).
    """
    rng = np.random.default_rng(seed)
    catalog = build_catalog(rng)

    dates = [start + timedelta(days=offset) for offset in range(days)]
    records: list[dict] = []

    for _, product in catalog.iterrows():
        base_demand = product["base_demand"]
        elasticity = product["elasticity"]
        base_price = product["base_price"]

        for store_code, (_city, store_multiplier) in STORES.items():
            # Each (product, store) pair gets its own promotion calendar.
            promo_days = set(
                rng.choice(days, size=max(1, int(days * 0.06)), replace=False).tolist()
            )

            for day_index, day in enumerate(dates):
                promotion = day_index in promo_days
                holiday = _is_holiday(day)

                # Promotions are a genuine price cut, so their effect reaches
                # demand through elasticity as well as through the flag.
                price = base_price * (0.75 if promotion else 1.0)
                price = round(price, 2)

                weekday_factor = 1.28 if day.weekday() >= 5 else 0.94
                annual_factor = 1.0 + 0.18 * math.sin(
                    2 * math.pi * day.timetuple().tm_yday / 365.0
                )
                festive_factor = 1.35 if day.month in FESTIVE_MONTHS else 1.0
                holiday_factor = 1.45 if holiday else 1.0
                trend_factor = 1.0 + 0.12 * (day_index / max(1, days))

                price_ratio = price / base_price
                # Constant-elasticity demand: demand scales as price^elasticity.
                elasticity_factor = price_ratio**elasticity

                expected = (
                    base_demand
                    * store_multiplier
                    * weekday_factor
                    * annual_factor
                    * festive_factor
                    * holiday_factor
                    * trend_factor
                    * elasticity_factor
                )
                # Poisson: unit sales are counts. This is the irreducible noise
                # floor -- no model can predict below it.
                units = int(rng.poisson(max(0.05, expected)))

                records.append(
                    {
                        "date": day,
                        "product_id": product["product_id"],
                        "store_id": store_code,
                        "category": product["category"],
                        "units_sold": units,
                        "price": price,
                        "promotion": int(promotion),
                        "holiday": int(holiday),
                    }
                )

    sales = pd.DataFrame.from_records(records)
    sales["date"] = pd.to_datetime(sales["date"])
    return sales.sort_values(["product_id", "store_id", "date"]).reset_index(drop=True), catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic retail sales data.")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--days", type=int, default=730, help="Two years by default.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", default="data/generated")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(args.start)
    sales, catalog = generate(start, args.days, args.seed)

    sales_path = out_dir / "sales.csv"
    catalog_path = out_dir / "catalog.csv"
    sales.to_csv(sales_path, index=False)
    catalog.to_csv(catalog_path, index=False)

    print(f"Wrote {len(sales):,} sales rows to {sales_path}")
    print(f"Wrote {len(catalog):,} catalog rows to {catalog_path}")
    print(
        f"  {sales['product_id'].nunique()} products x {sales['store_id'].nunique()} stores "
        f"x {sales['date'].nunique()} days"
    )
    print(f"  date range: {sales['date'].min().date()} to {sales['date'].max().date()}")
    print(f"  mean daily units: {sales['units_sold'].mean():.2f}")
    print(f"  promotion days: {sales['promotion'].mean() * 100:.1f}% of rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
