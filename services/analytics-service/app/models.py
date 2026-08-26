"""Analytics persistence model.

This is a **read model**, built from events rather than owning transactional
state. It is the one place in the platform where denormalisation is correct:
answering "revenue by category last month" from the order service's normalised
tables would mean a cross-service join that cannot exist, and would put
analytical scans on the same database serving checkout.

Two layers:

* ``sales_facts`` -- one immutable row per order line, written when an order is
  confirmed. Append-only, so it can be replayed and re-aggregated.
* ``daily_aggregates`` -- pre-computed rollups. Dashboards read these, so a
  chart never scans the fact table.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from retailpulse_common.db import Base

# Registers `processed_events` and `outbox_events` on this service's metadata.
from retailpulse_common.events.idempotency import ProcessedEvent  # noqa: F401,E402
from retailpulse_common.events.outbox import OutboxEvent  # noqa: F401,E402


class SalesFact(Base):
    """One order line, frozen at the moment the order was confirmed."""

    __tablename__ = "sales_facts"

    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="Uncategorised")
    store_id: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # The business date, separate from the row's insertion time. Late-arriving
    # events must land on the day the sale happened, not the day it was
    # processed, or every aggregate silently drifts.
    sale_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # One fact per (order, product). The idempotency table stops duplicate
        # events; this stops everything else.
        UniqueConstraint("order_id", "product_id", name="uq_sales_facts_order_product"),
        CheckConstraint("quantity > 0", name="ck_sales_facts_quantity_positive"),
        CheckConstraint("revenue >= 0", name="ck_sales_facts_revenue_non_negative"),
        Index("ix_sales_facts_date_category", "sale_date", "category"),
        Index("ix_sales_facts_date_store", "sale_date", "store_id"),
        Index("ix_sales_facts_product_date", "product_id", "sale_date"),
    )


class DailyAggregate(Base):
    """Pre-computed daily rollup per (date, product, store)."""

    __tablename__ = "daily_aggregates"

    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sale_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    store_id: Mapped[str] = mapped_column(String(16), nullable=False)

    units_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Recomputing a day must update its rows, never append a second copy.
        UniqueConstraint(
            "sale_date", "product_id", "store_id", name="uq_daily_aggregates_grain"
        ),
        CheckConstraint("units_sold >= 0", name="ck_daily_aggregates_units_non_negative"),
        Index("ix_daily_aggregates_date_category", "sale_date", "category"),
    )


class OrderEventFact(Base):
    """Order lifecycle counts, for fulfilment and cancellation rates.

    Kept separate from sales_facts because an order has one lifecycle but many
    lines -- mixing them would double-count every rate by the number of items
    in the basket.
    """

    __tablename__ = "order_event_facts"

    event_fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    reason: Mapped[str | None] = mapped_column(String(120))
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("order_id", "status", name="uq_order_event_facts_order_status"),
        Index("ix_order_event_facts_status_date", "status", "occurred_on"),
    )
