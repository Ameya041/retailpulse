"""Analytics API contracts."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class DashboardMetrics(BaseModel):
    """Headline numbers for the admin dashboard."""

    total_orders: int
    orders_today: int
    total_revenue: Decimal
    revenue_today: Decimal
    average_order_value: Decimal
    total_units_sold: int
    fulfilment_rate_pct: float
    cancellation_rate_pct: float
    # Owned by the inventory service. Null when it is unreachable, rather than
    # zero -- reporting an inventory value of 0 during an outage would be a
    # lie, and someone would act on it.
    inventory_value: Decimal | None = None
    low_stock_products: int | None = None
    currency: str = "INR"
    window_days: int


class SalesByProduct(BaseModel):
    product_id: uuid.UUID
    sku: str
    category: str
    units_sold: int
    revenue: Decimal
    units_per_day: float
    revenue_per_day: float


class SalesByCategory(BaseModel):
    category: str
    units_sold: int
    revenue: Decimal
    order_count: int
    revenue_share_pct: float


class SalesByStore(BaseModel):
    store_id: str
    units_sold: int
    revenue: Decimal
    order_count: int
    average_order_value: Decimal | None


class SalesOverTimePoint(BaseModel):
    date: date
    units_sold: int
    revenue: Decimal
    order_count: int


class WeeklySales(BaseModel):
    week_start: date
    category: str
    units_sold: int
    revenue: Decimal
    order_count: int


class InventoryTurnover(BaseModel):
    sku: str
    category: str
    units_sold: int
    units_per_day: float
    stock_on_hand: int
    annual_turnover: float | None


class ReplenishmentItem(BaseModel):
    product_id: str
    store_id: str
    current_stock: int
    predicted_demand_7d: int
    recommended_order_quantity: int
    urgency: str
    days_of_cover: float | None


class ReplenishmentReport(BaseModel):
    """Products needing a reorder, worst first."""

    items: list[ReplenishmentItem]
    generated_at: str
    model_version: str | None
    # Set when the ML service could not be reached, so an empty list is never
    # mistaken for "nothing needs reordering".
    degraded_reason: str | None = None


class AggregateRebuildResponse(BaseModel):
    rows_written: int
    since: date | None
    until: date | None


class TopProduct(BaseModel):
    sku: str
    product_name: str
    category: str
    units_sold: int
    revenue: Decimal


class WindowQuery(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
