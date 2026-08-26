"""The Pandas aggregation pipeline.

Reads raw facts, computes the rollups a dashboard needs, and writes them back.

**Why Pandas rather than SQL.** The rollups here are simple enough for SQL, and
in a production warehouse they would be SQL (or dbt). Pandas is used because
the same frames feed the ML feature pipeline, and computing product velocity
and inventory turnover in one place keeps a single definition of each metric.
Two definitions of "velocity" -- one in SQL for the dashboard, one in Python
for the model -- is how a dashboard and a model end up disagreeing.

**Why recompute rather than increment.** The aggregate table is rebuilt for a
date range rather than incremented per event. Incremental counters drift when
an event is replayed or arrives late, and the drift is invisible. Recomputing
from the immutable fact table is idempotent by construction: running it twice
produces the same answer.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DailyAggregate, SalesFact

logger = logging.getLogger("analytics-service")


def load_facts(session: Session, *, since: date | None = None, until: date | None = None) -> pd.DataFrame:
    """Pull sales facts into a DataFrame."""
    stmt = select(
        SalesFact.sale_date,
        SalesFact.order_id,
        SalesFact.product_id,
        SalesFact.sku,
        SalesFact.product_name,
        SalesFact.category,
        SalesFact.store_id,
        SalesFact.quantity,
        SalesFact.revenue,
    )
    if since is not None:
        stmt = stmt.where(SalesFact.sale_date >= since)
    if until is not None:
        stmt = stmt.where(SalesFact.sale_date <= until)

    rows = session.execute(stmt).all()
    frame = pd.DataFrame(rows, columns=[
        "sale_date", "order_id", "product_id", "sku", "product_name",
        "category", "store_id", "quantity", "revenue",
    ])
    if frame.empty:
        return frame

    frame["sale_date"] = pd.to_datetime(frame["sale_date"])
    # Decimal survives the round trip but does not arithmetic well in Pandas.
    # Converted once here, and every value that leaves the pipeline is turned
    # back into Decimal before it reaches the database or the API.
    frame["revenue"] = frame["revenue"].astype(float)
    frame["quantity"] = frame["quantity"].astype(int)
    return frame


def daily_rollup(facts: pd.DataFrame) -> pd.DataFrame:
    """Units, revenue and distinct orders per (date, product, store)."""
    if facts.empty:
        return pd.DataFrame()

    grouped = (
        facts.groupby(["sale_date", "product_id", "sku", "category", "store_id"], observed=True)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            # Distinct orders, not row count: one order with three lines is
            # one order.
            order_count=("order_id", "nunique"),
        )
        .reset_index()
    )
    return grouped


def weekly_rollup(facts: pd.DataFrame) -> pd.DataFrame:
    """Weekly totals, weeks starting Monday."""
    if facts.empty:
        return pd.DataFrame()

    frame = facts.copy()
    frame["week_start"] = frame["sale_date"] - pd.to_timedelta(
        frame["sale_date"].dt.dayofweek, unit="D"
    )
    return (
        frame.groupby(["week_start", "category"], observed=True)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            order_count=("order_id", "nunique"),
        )
        .reset_index()
    )


def product_velocity(facts: pd.DataFrame, *, days: int) -> pd.DataFrame:
    """Average units sold per day, per product.

    Divided by the length of the *window*, not by the number of days the
    product happened to sell on. Dividing by selling days would make a product
    that sold 10 units on one day of a month look like it sells 10 a day.
    """
    if facts.empty or days <= 0:
        return pd.DataFrame()

    grouped = (
        facts.groupby(["product_id", "sku", "category"], observed=True)
        .agg(units_sold=("quantity", "sum"), revenue=("revenue", "sum"))
        .reset_index()
    )
    grouped["units_per_day"] = (grouped["units_sold"] / days).round(3)
    grouped["revenue_per_day"] = (grouped["revenue"] / days).round(2)
    return grouped.sort_values("units_sold", ascending=False).reset_index(drop=True)


def store_performance(facts: pd.DataFrame) -> pd.DataFrame:
    if facts.empty:
        return pd.DataFrame()

    grouped = (
        facts.groupby("store_id", observed=True)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            order_count=("order_id", "nunique"),
        )
        .reset_index()
    )
    grouped["average_order_value"] = (
        grouped["revenue"] / grouped["order_count"].replace(0, pd.NA)
    ).round(2)
    return grouped.sort_values("revenue", ascending=False).reset_index(drop=True)


def category_performance(facts: pd.DataFrame) -> pd.DataFrame:
    if facts.empty:
        return pd.DataFrame()

    grouped = (
        facts.groupby("category", observed=True)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            order_count=("order_id", "nunique"),
        )
        .reset_index()
    )
    total_revenue = grouped["revenue"].sum()
    grouped["revenue_share_pct"] = (
        (grouped["revenue"] / total_revenue * 100).round(2) if total_revenue else 0.0
    )
    return grouped.sort_values("revenue", ascending=False).reset_index(drop=True)


def inventory_turnover(facts: pd.DataFrame, stock_on_hand: dict[str, int], *, days: int) -> pd.DataFrame:
    """Annualised turnover per product: how many times stock sells through.

    Turnover = (units sold per day x 365) / units held. A product with high
    turnover is working; one near zero is capital sitting on a shelf.
    """
    if facts.empty or days <= 0:
        return pd.DataFrame()

    velocity = product_velocity(facts, days=days)
    velocity["stock_on_hand"] = velocity["sku"].map(stock_on_hand).fillna(0).astype(int)
    velocity["annual_turnover"] = velocity.apply(
        lambda row: round(row["units_per_day"] * 365 / row["stock_on_hand"], 2)
        if row["stock_on_hand"] > 0
        else None,
        axis=1,
    )
    return velocity


def sales_over_time(facts: pd.DataFrame) -> pd.DataFrame:
    """Daily totals across the whole business, gap-filled.

    Days with no sales are emitted as zero rather than omitted: a chart that
    silently skips empty days compresses the x-axis and makes a quiet week look
    like a busy one.
    """
    if facts.empty:
        return pd.DataFrame()

    daily = (
        facts.groupby("sale_date", observed=True)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            order_count=("order_id", "nunique"),
        )
        .reset_index()
    )

    full_range = pd.date_range(daily["sale_date"].min(), daily["sale_date"].max(), freq="D")
    daily = (
        daily.set_index("sale_date")
        .reindex(full_range, fill_value=0)
        .rename_axis("sale_date")
        .reset_index()
    )
    return daily


def rebuild_daily_aggregates(
    session: Session, *, since: date | None = None, until: date | None = None
) -> int:
    """Recompute and replace the daily aggregate rows for a date range.

    Delete-then-insert for the affected range, so running it twice is
    identical to running it once. That property is what makes the job safe to
    re-run after a backfill or a replay.
    """
    facts = load_facts(session, since=since, until=until)
    if facts.empty:
        logger.info("no facts to aggregate", extra={"since": str(since), "until": str(until)})
        return 0

    rollup = daily_rollup(facts)

    delete_stmt = delete(DailyAggregate)
    if since is not None:
        delete_stmt = delete_stmt.where(DailyAggregate.sale_date >= since)
    if until is not None:
        delete_stmt = delete_stmt.where(DailyAggregate.sale_date <= until)
    session.execute(delete_stmt)

    session.add_all(
        [
            DailyAggregate(
                sale_date=row["sale_date"].date(),
                product_id=row["product_id"],
                sku=row["sku"],
                category=row["category"],
                store_id=row["store_id"],
                units_sold=int(row["units_sold"]),
                revenue=Decimal(str(round(row["revenue"], 2))),
                order_count=int(row["order_count"]),
            )
            for _, row in rollup.iterrows()
        ]
    )
    session.flush()

    logger.info("daily aggregates rebuilt", extra={"rows": len(rollup)})
    return len(rollup)


def default_window(days: int = 30) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=days), today
