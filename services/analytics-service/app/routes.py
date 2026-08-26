"""Analytics routes.

Every endpoint requires staff. Aggregate revenue and store performance are
commercially sensitive, and there is no customer-facing use for them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.clients import ForecastClient, InventoryClient
from app.deps import get_db_session, get_forecast_client, get_inventory_client, require_roles
from app.schemas import (
    AggregateRebuildResponse,
    DashboardMetrics,
    InventoryTurnover,
    ReplenishmentItem,
    ReplenishmentReport,
    SalesByCategory,
    SalesByProduct,
    SalesByStore,
    SalesOverTimePoint,
    TopProduct,
    WeeklySales,
)
from app.service import AnalyticsService
from retailpulse_common.auth import Role, TokenPayload

router = APIRouter(prefix="/analytics", tags=["analytics"])

SessionDep = Annotated[Session, Depends(get_db_session)]
InventoryDep = Annotated[InventoryClient, Depends(get_inventory_client)]
ForecastDep = Annotated[ForecastClient, Depends(get_forecast_client)]
StaffDep = Annotated[
    TokenPayload, Depends(require_roles(Role.ADMIN, Role.WAREHOUSE_OPERATOR))
]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]

WindowDep = Annotated[int, Query(ge=1, le=365, description="Lookback window in days.")]

AUTH_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Requires ADMIN or WAREHOUSE_OPERATOR."},
}


@router.get(
    "/dashboard",
    response_model=DashboardMetrics,
    summary="Headline dashboard metrics",
    responses=AUTH_ERRORS,
)
def dashboard(
    session: SessionDep, inventory: InventoryDep, _: StaffDep, days: WindowDep = 30
) -> DashboardMetrics:
    """Orders, revenue, fulfilment and cancellation rates for the window.

    Inventory figures come from the inventory service and are **null** rather
    than zero when it is unreachable -- a dashboard reporting zero stock during
    an outage would be acted on.
    """
    metrics = AnalyticsService(session).dashboard(days=days, inventory=inventory.summary())
    return DashboardMetrics(**metrics)


@router.get(
    "/sales/by-product",
    response_model=list[SalesByProduct],
    summary="Sales and velocity per product",
    responses=AUTH_ERRORS,
)
def sales_by_product(
    session: SessionDep,
    _: StaffDep,
    days: WindowDep = 30,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[SalesByProduct]:
    """Units and revenue per product, plus units-per-day velocity.

    Velocity divides by the window length, not by the days a product happened
    to sell on -- otherwise a product that sold 10 units on a single day would
    look like it sells 10 a day.
    """
    return [SalesByProduct(**row) for row in AnalyticsService(session).sales_by_product(days=days, limit=limit)]


@router.get(
    "/sales/by-category",
    response_model=list[SalesByCategory],
    summary="Sales per category",
    responses=AUTH_ERRORS,
)
def sales_by_category(session: SessionDep, _: StaffDep, days: WindowDep = 30) -> list[SalesByCategory]:
    """Revenue and share of total per category."""
    return [SalesByCategory(**row) for row in AnalyticsService(session).sales_by_category(days=days)]


@router.get(
    "/sales/by-store",
    response_model=list[SalesByStore],
    summary="Sales per store",
    responses=AUTH_ERRORS,
)
def sales_by_store(session: SessionDep, _: StaffDep, days: WindowDep = 30) -> list[SalesByStore]:
    """Revenue, orders and average order value per location."""
    return [SalesByStore(**row) for row in AnalyticsService(session).sales_by_store(days=days)]


@router.get(
    "/sales/over-time",
    response_model=list[SalesOverTimePoint],
    summary="Daily sales over time",
    responses=AUTH_ERRORS,
)
def sales_over_time(session: SessionDep, _: StaffDep, days: WindowDep = 30) -> list[SalesOverTimePoint]:
    """Daily totals, gap-filled.

    Days with no sales appear as zero rather than being omitted; a chart that
    skips empty days compresses its axis and makes a quiet week look busy.
    """
    return [
        SalesOverTimePoint(
            date=row["sale_date"],
            units_sold=int(row["units_sold"]),
            revenue=row["revenue"],
            order_count=int(row["order_count"]),
        )
        for row in AnalyticsService(session).sales_over_time(days=days)
    ]


@router.get(
    "/sales/weekly",
    response_model=list[WeeklySales],
    summary="Weekly sales per category",
    responses=AUTH_ERRORS,
)
def weekly_sales(session: SessionDep, _: StaffDep, days: WindowDep = 90) -> list[WeeklySales]:
    """Weeks start on Monday."""
    return [
        WeeklySales(
            week_start=row["week_start"],
            category=row["category"],
            units_sold=int(row["units_sold"]),
            revenue=row["revenue"],
            order_count=int(row["order_count"]),
        )
        for row in AnalyticsService(session).weekly_sales(days=days)
    ]


@router.get(
    "/top-products",
    response_model=list[TopProduct],
    summary="Best sellers",
    responses=AUTH_ERRORS,
)
def top_products(
    session: SessionDep,
    _: StaffDep,
    days: WindowDep = 30,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[TopProduct]:
    """Ranked by units sold."""
    return [TopProduct(**row) for row in AnalyticsService(session).top_products(days=days, limit=limit)]


@router.get(
    "/inventory-turnover",
    response_model=list[InventoryTurnover],
    summary="Annualised inventory turnover",
    responses=AUTH_ERRORS,
)
def inventory_turnover(
    session: SessionDep, _: StaffDep, days: WindowDep = 30
) -> list[InventoryTurnover]:
    """How many times a year each product's stock sells through.

    Stock levels are owned by the inventory service; until that lookup is
    wired in, turnover is reported as null rather than assumed.
    """
    rows = AnalyticsService(session).inventory_turnover({}, days=days)
    return [
        InventoryTurnover(
            sku=row["sku"],
            category=row["category"],
            units_sold=int(row["units_sold"]),
            units_per_day=float(row["units_per_day"]),
            stock_on_hand=int(row["stock_on_hand"]),
            annual_turnover=row["annual_turnover"],
        )
        for row in rows
    ]


@router.get(
    "/replenishment",
    response_model=ReplenishmentReport,
    summary="Products needing a reorder",
    responses=AUTH_ERRORS,
)
def replenishment(
    session: SessionDep,
    forecast: ForecastDep,
    _: StaffDep,
    days: WindowDep = 30,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReplenishmentReport:
    """Forecast-driven reorder list, most urgent first.

    Combines what analytics knows (what sells) with what the ML service
    predicts (what will sell). If the ML service is unreachable the report says
    so explicitly, so an empty list is never mistaken for 'nothing to reorder'.
    """
    products = AnalyticsService(session).sales_by_product(days=days, limit=limit)

    items: list[ReplenishmentItem] = []
    model_version: str | None = None
    degraded: str | None = None

    for product in products:
        recommendation = forecast.replenishment(product["sku"], "BLR01", 0, 0)
        if recommendation is None:
            degraded = "The forecasting service is unavailable; this report is incomplete."
            break
        model_version = recommendation.get("model_version")
        items.append(
            ReplenishmentItem(
                product_id=recommendation["product_id"],
                store_id=recommendation["store_id"],
                current_stock=recommendation["current_stock"],
                predicted_demand_7d=recommendation["predicted_demand_7d"],
                recommended_order_quantity=recommendation["recommended_order_quantity"],
                urgency=recommendation["urgency"],
                days_of_cover=recommendation.get("days_of_cover"),
            )
        )

    urgency_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "NONE": 3}
    items.sort(key=lambda item: urgency_order.get(item.urgency, 99))

    return ReplenishmentReport(
        items=items,
        generated_at=datetime.now(UTC).isoformat(),
        model_version=model_version,
        degraded_reason=degraded,
    )


@router.post(
    "/aggregates/rebuild",
    response_model=AggregateRebuildResponse,
    summary="Recompute daily aggregates",
    responses={**AUTH_ERRORS, 403: {"description": "Requires ADMIN."}},
)
def rebuild_aggregates(
    session: SessionDep,
    _: AdminDep,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
) -> AggregateRebuildResponse:
    """Rebuild the aggregate table from the immutable facts.

    Idempotent by construction: the range is deleted and recomputed, so running
    it twice gives the same answer. Safe to re-run after a backfill or an event
    replay -- which is exactly why aggregates are recomputed rather than
    incremented.
    """
    rows = AnalyticsService(session).rebuild_aggregates(since=since, until=until)
    return AggregateRebuildResponse(rows_written=rows, since=since, until=until)
