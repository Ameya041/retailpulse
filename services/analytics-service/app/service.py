"""Analytics business logic."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import pipeline
from app.models import OrderEventFact, SalesFact

logger = logging.getLogger("analytics-service")

# Order statuses that mean the goods reached the customer, and those that mean
# the order ended without a sale. Defined once so the fulfilment and
# cancellation rates cannot drift apart.
FULFILLED_STATUSES = ("DELIVERED",)
CANCELLED_STATUSES = ("CANCELLED",)
TERMINAL_STATUSES = FULFILLED_STATUSES + CANCELLED_STATUSES


class AnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------
    def record_sale_lines(self, lines: list[dict]) -> int:
        """Insert sales facts, skipping any that already exist."""
        written = 0
        for line in lines:
            existing = self.session.scalar(
                select(SalesFact).where(
                    SalesFact.order_id == line["order_id"],
                    SalesFact.product_id == line["product_id"],
                )
            )
            if existing is not None:
                continue
            self.session.add(SalesFact(**line))
            written += 1
        self.session.flush()
        return written

    def record_order_event(
        self, order_id, status: str, total_amount: Decimal, currency: str, occurred_on: date, reason: str | None
    ) -> bool:
        existing = self.session.scalar(
            select(OrderEventFact).where(
                OrderEventFact.order_id == order_id, OrderEventFact.status == status
            )
        )
        if existing is not None:
            return False
        self.session.add(
            OrderEventFact(
                order_id=order_id,
                status=status,
                total_amount=total_amount,
                currency=currency,
                occurred_on=occurred_on,
                reason=reason,
            )
        )
        self.session.flush()
        return True

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def dashboard(self, *, days: int = 30, inventory: dict | None = None) -> dict:
        since = date.today() - timedelta(days=days)
        today = date.today()

        totals = self.session.execute(
            select(
                func.count(func.distinct(SalesFact.order_id)),
                func.coalesce(func.sum(SalesFact.revenue), 0),
                func.coalesce(func.sum(SalesFact.quantity), 0),
            ).where(SalesFact.sale_date >= since)
        ).one()
        total_orders, total_revenue, total_units = totals

        today_totals = self.session.execute(
            select(
                func.count(func.distinct(SalesFact.order_id)),
                func.coalesce(func.sum(SalesFact.revenue), 0),
            ).where(SalesFact.sale_date == today)
        ).one()
        orders_today, revenue_today = today_totals

        total_revenue = Decimal(str(total_revenue or 0))
        average_order_value = (
            (total_revenue / total_orders).quantize(Decimal("0.01"))
            if total_orders
            else Decimal("0.00")
        )

        return {
            "total_orders": int(total_orders or 0),
            "orders_today": int(orders_today or 0),
            "total_revenue": total_revenue,
            "revenue_today": Decimal(str(revenue_today or 0)),
            "average_order_value": average_order_value,
            "total_units_sold": int(total_units or 0),
            "fulfilment_rate_pct": self.fulfilment_rate(since),
            "cancellation_rate_pct": self.cancellation_rate(since),
            "inventory_value": (inventory or {}).get("inventory_value"),
            "low_stock_products": (inventory or {}).get("low_stock_products"),
            "window_days": days,
        }

    def _status_counts(self, since: date) -> dict[str, int]:
        rows = self.session.execute(
            select(OrderEventFact.status, func.count())
            .where(OrderEventFact.occurred_on >= since)
            .group_by(OrderEventFact.status)
        ).all()
        return {status: int(count) for status, count in rows}

    def fulfilment_rate(self, since: date) -> float:
        """Delivered as a share of orders that *finished*.

        The denominator is deliberately terminal orders only. Including orders
        still in flight would make the rate depend on how many orders happen to
        be mid-flight right now, so it would drop every time business was good.
        """
        counts = self._status_counts(since)
        delivered = sum(counts.get(s, 0) for s in FULFILLED_STATUSES)
        terminal = sum(counts.get(s, 0) for s in TERMINAL_STATUSES)
        return round(delivered / terminal * 100, 2) if terminal else 0.0

    def cancellation_rate(self, since: date) -> float:
        counts = self._status_counts(since)
        cancelled = sum(counts.get(s, 0) for s in CANCELLED_STATUSES)
        terminal = sum(counts.get(s, 0) for s in TERMINAL_STATUSES)
        return round(cancelled / terminal * 100, 2) if terminal else 0.0

    # ------------------------------------------------------------------
    # Breakdowns
    # ------------------------------------------------------------------
    def _facts(self, days: int):
        since = date.today() - timedelta(days=days)
        return pipeline.load_facts(self.session, since=since), days

    def sales_by_product(self, *, days: int = 30, limit: int = 50) -> list[dict]:
        facts, window = self._facts(days)
        frame = pipeline.product_velocity(facts, days=window)
        return frame.head(limit).to_dict("records") if not frame.empty else []

    def sales_by_category(self, *, days: int = 30) -> list[dict]:
        facts, _ = self._facts(days)
        frame = pipeline.category_performance(facts)
        return frame.to_dict("records") if not frame.empty else []

    def sales_by_store(self, *, days: int = 30) -> list[dict]:
        facts, _ = self._facts(days)
        frame = pipeline.store_performance(facts)
        return frame.to_dict("records") if not frame.empty else []

    def sales_over_time(self, *, days: int = 30) -> list[dict]:
        facts, _ = self._facts(days)
        frame = pipeline.sales_over_time(facts)
        if frame.empty:
            return []
        frame["sale_date"] = frame["sale_date"].dt.date
        return frame.to_dict("records")

    def weekly_sales(self, *, days: int = 90) -> list[dict]:
        facts, _ = self._facts(days)
        frame = pipeline.weekly_rollup(facts)
        if frame.empty:
            return []
        frame["week_start"] = frame["week_start"].dt.date
        return frame.to_dict("records")

    def top_products(self, *, days: int = 30, limit: int = 10) -> list[dict]:
        since = date.today() - timedelta(days=days)
        rows = self.session.execute(
            select(
                SalesFact.sku,
                SalesFact.product_name,
                SalesFact.category,
                func.sum(SalesFact.quantity).label("units_sold"),
                func.sum(SalesFact.revenue).label("revenue"),
            )
            .where(SalesFact.sale_date >= since)
            .group_by(SalesFact.sku, SalesFact.product_name, SalesFact.category)
            .order_by(func.sum(SalesFact.quantity).desc())
            .limit(limit)
        ).all()
        return [
            {
                "sku": r.sku,
                "product_name": r.product_name,
                "category": r.category,
                "units_sold": int(r.units_sold),
                "revenue": Decimal(str(r.revenue)),
            }
            for r in rows
        ]

    def inventory_turnover(self, stock_on_hand: dict[str, int], *, days: int = 30) -> list[dict]:
        facts, window = self._facts(days)
        frame = pipeline.inventory_turnover(facts, stock_on_hand, days=window)
        return frame.to_dict("records") if not frame.empty else []

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------
    def rebuild_aggregates(self, *, since: date | None = None, until: date | None = None) -> int:
        return pipeline.rebuild_daily_aggregates(self.session, since=since, until=until)
