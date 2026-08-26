"""Analytics service tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, timedelta
from decimal import Decimal

import pytest

from app.handlers import (
    CONSUMER_GROUP,
    handle_order_confirmed,
    handle_order_delivered,
)
from app.models import DailyAggregate, OrderEventFact, SalesFact
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import DuplicateEventError, IdempotencyGuard
from retailpulse_common.events.topics import EventType, Topic
from tests.conftest import WIDGET_ID


def order_confirmed(order_id=None, *, lines=None, timestamp=None) -> EventEnvelope:
    payload = {
        "order_id": str(order_id or uuid.uuid4()),
        "customer_id": str(uuid.uuid4()),
        "total_amount": "400.00",
        "currency": "INR",
        "lines": lines
        or [
            {
                "product_id": str(WIDGET_ID),
                "sku": "WIDGET-001",
                "product_name": "Standard Widget",
                "category": "Electronics",
                "store_id": "BLR01",
                "quantity": 2,
                "unit_price": "200.00",
            }
        ],
    }
    event = EventEnvelope(
        event_type=EventType.ORDER_CONFIRMED, source="order-service", payload=payload
    )
    if timestamp is not None:
        event = event.model_copy(update={"timestamp": timestamp})
    return event


# ---------------------------------------------------------------------------
# Recording facts
# ---------------------------------------------------------------------------
def test_confirmed_order_is_recorded_as_a_sale(database):
    with database.session() as session:
        handle_order_confirmed(order_confirmed(), Topic.ORDER_CONFIRMED, session=session)

    with database.session() as session:
        fact = session.query(SalesFact).one()
        assert fact.sku == "WIDGET-001"
        assert fact.quantity == 2
        assert fact.revenue == Decimal("400.00")


def test_revenue_is_quantity_times_unit_price(database):
    with database.session() as session:
        handle_order_confirmed(
            order_confirmed(
                lines=[
                    {
                        "product_id": str(WIDGET_ID),
                        "sku": "W",
                        "quantity": 3,
                        "unit_price": "19.99",
                    }
                ]
            ),
            Topic.ORDER_CONFIRMED,
            session=session,
        )

    with database.session() as session:
        assert session.query(SalesFact).one().revenue == Decimal("59.97")


def test_multi_line_orders_produce_one_fact_per_line(database):
    lines = [
        {"product_id": str(uuid.uuid4()), "sku": f"SKU-{i}", "quantity": 1, "unit_price": "10.00"}
        for i in range(3)
    ]
    with database.session() as session:
        handle_order_confirmed(
            order_confirmed(lines=lines), Topic.ORDER_CONFIRMED, session=session
        )

    with database.session() as session:
        assert session.query(SalesFact).count() == 3


def test_the_sale_date_comes_from_the_event_not_from_today(database):
    """A replayed event must land on the day the sale happened."""
    from datetime import datetime

    two_weeks_ago = datetime.now(UTC) - timedelta(days=14)

    with database.session() as session:
        handle_order_confirmed(
            order_confirmed(timestamp=two_weeks_ago), Topic.ORDER_CONFIRMED, session=session
        )

    with database.session() as session:
        assert session.query(SalesFact).one().sale_date == two_weeks_ago.date()


def test_a_redelivered_event_does_not_double_count_revenue(database):
    event = order_confirmed()

    with database.session() as session:
        handle_order_confirmed(event, Topic.ORDER_CONFIRMED, session=session)

    with pytest.raises(DuplicateEventError), database.session() as session:
        handle_order_confirmed(event, Topic.ORDER_CONFIRMED, session=session)

    with database.session() as session:
        assert session.query(SalesFact).count() == 1


def test_a_new_event_for_the_same_order_still_does_not_duplicate(database):
    """Defence in depth: the unique index catches what idempotency misses."""
    order_id = uuid.uuid4()

    for _ in range(3):
        with database.session() as session:
            handle_order_confirmed(
                order_confirmed(order_id), Topic.ORDER_CONFIRMED, session=session
            )

    with database.session() as session:
        assert session.query(SalesFact).count() == 1


def test_analytics_uses_its_own_consumer_group(database):
    """Fulfilment also consumes ORDER_CONFIRMED; neither may suppress the other."""
    event = order_confirmed()
    with database.session() as session:
        handle_order_confirmed(event, Topic.ORDER_CONFIRMED, session=session)

    with database.session() as session:
        assert IdempotencyGuard(session, CONSUMER_GROUP).has_processed(event.event_id)
        assert not IdempotencyGuard(session, "fulfilment-service").has_processed(event.event_id)


def test_malformed_line_fails_permanently(database):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_confirmed(
            order_confirmed(lines=[{"sku": "no-product-id", "quantity": 1}]),
            Topic.ORDER_CONFIRMED,
            session=session,
        )


def test_missing_order_id_fails_permanently(database):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_confirmed(
            EventEnvelope(
                event_type=EventType.ORDER_CONFIRMED, source="order-service", payload={}
            ),
            Topic.ORDER_CONFIRMED,
            session=session,
        )


def test_lifecycle_events_are_recorded(database):
    order_id = uuid.uuid4()
    with database.session() as session:
        handle_order_delivered(
            EventEnvelope(
                event_type=EventType.ORDER_DELIVERED,
                source="fulfilment-service",
                payload={"order_id": str(order_id), "total_amount": "99.00"},
            ),
            Topic.ORDER_DELIVERED,
            session=session,
        )

    with database.session() as session:
        assert session.query(OrderEventFact).one().status == "DELIVERED"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def test_dashboard_reports_totals(client, staff_headers, seeded):
    body = client.get("/analytics/dashboard?days=30", headers=staff_headers).json()

    # 2x100 + 3x100 + 1x500 + 4x100 = 1400
    assert Decimal(body["total_revenue"]) == Decimal("1400.00")
    assert body["total_units_sold"] == 10
    assert body["total_orders"] == 4


def test_average_order_value_is_revenue_over_orders(client, staff_headers, seeded):
    body = client.get("/analytics/dashboard?days=30", headers=staff_headers).json()
    assert Decimal(body["average_order_value"]) == Decimal("350.00")


def test_fulfilment_and_cancellation_rates_use_terminal_orders(client, staff_headers, seeded):
    """8 delivered, 2 cancelled -> 80% / 20%."""
    body = client.get("/analytics/dashboard?days=30", headers=staff_headers).json()

    assert body["fulfilment_rate_pct"] == 80.0
    assert body["cancellation_rate_pct"] == 20.0


def test_rates_are_zero_rather_than_undefined_with_no_orders(client, staff_headers):
    body = client.get("/analytics/dashboard", headers=staff_headers).json()
    assert body["fulfilment_rate_pct"] == 0.0
    assert body["cancellation_rate_pct"] == 0.0


def test_dashboard_includes_inventory_figures_when_available(client, staff_headers, seeded):
    body = client.get("/analytics/dashboard", headers=staff_headers).json()
    assert body["low_stock_products"] == 3


def test_inventory_figures_are_null_not_zero_when_unavailable(client, staff_headers, seeded):
    """Reporting zero stock during an outage would be acted on."""
    from app.clients import NullInventoryClient
    from app.deps import get_inventory_client
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_inventory_client] = NullInventoryClient
    try:
        body = client.get("/analytics/dashboard", headers=staff_headers).json()
    finally:
        fastapi_app.dependency_overrides.pop(get_inventory_client, None)

    assert body["low_stock_products"] is None
    assert body["inventory_value"] is None


def test_window_excludes_older_sales(client, staff_headers, database):
    with database.session() as session:
        from tests.conftest import make_fact

        session.add(make_fact(days_ago=200, quantity=99))

    body = client.get("/analytics/dashboard?days=30", headers=staff_headers).json()
    assert body["total_units_sold"] == 0


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------
def test_sales_by_category(client, staff_headers, seeded):
    rows = client.get("/analytics/sales/by-category?days=30", headers=staff_headers).json()
    by_category = {row["category"]: row for row in rows}

    assert Decimal(by_category["Electronics"]["revenue"]) == Decimal("900.00")
    assert Decimal(by_category["Home Appliances"]["revenue"]) == Decimal("500.00")


def test_category_revenue_shares_sum_to_one_hundred(client, staff_headers, seeded):
    rows = client.get("/analytics/sales/by-category?days=30", headers=staff_headers).json()
    assert sum(row["revenue_share_pct"] for row in rows) == pytest.approx(100.0, abs=0.05)


def test_sales_by_store(client, staff_headers, seeded):
    rows = client.get("/analytics/sales/by-store?days=30", headers=staff_headers).json()
    by_store = {row["store_id"]: row for row in rows}

    assert by_store["BLR01"]["units_sold"] == 7
    assert by_store["MAA01"]["units_sold"] == 3


def test_velocity_divides_by_the_window_not_by_selling_days(client, staff_headers, database):
    """Otherwise one big day looks like a permanently fast seller."""
    from tests.conftest import make_fact

    with database.session() as session:
        session.add(make_fact(days_ago=1, quantity=30))

    rows = client.get("/analytics/sales/by-product?days=30", headers=staff_headers).json()

    assert rows[0]["units_sold"] == 30
    assert rows[0]["units_per_day"] == pytest.approx(1.0)  # 30 / 30 days, not 30 / 1


def test_sales_over_time_fills_gaps_with_zero(client, staff_headers, seeded):
    """A chart that skips empty days makes a quiet week look busy."""
    rows = client.get("/analytics/sales/over-time?days=30", headers=staff_headers).json()

    dates = [date.fromisoformat(row["date"]) for row in rows]
    gaps = {(b - a).days for a, b in zip(dates[:-1], dates[1:], strict=True)}

    assert gaps == {1}
    assert any(row["units_sold"] == 0 for row in rows)


def test_top_products_are_ranked_by_units(client, staff_headers, seeded):
    rows = client.get("/analytics/top-products?days=30", headers=staff_headers).json()

    assert rows[0]["sku"] == "WIDGET-001"
    assert rows[0]["units_sold"] == 9


def test_weekly_sales_start_on_monday(client, staff_headers, seeded):
    rows = client.get("/analytics/sales/weekly?days=90", headers=staff_headers).json()
    assert all(date.fromisoformat(row["week_start"]).weekday() == 0 for row in rows)


def test_empty_database_returns_empty_lists_not_errors(client, staff_headers):
    for path in (
        "/analytics/sales/by-product",
        "/analytics/sales/by-category",
        "/analytics/sales/by-store",
        "/analytics/sales/over-time",
        "/analytics/top-products",
    ):
        response = client.get(path, headers=staff_headers)
        assert response.status_code == 200, path
        assert response.json() == []


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------
def test_rebuilding_aggregates_writes_rollups(client, admin_headers, seeded, database):
    response = client.post("/analytics/aggregates/rebuild", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["rows_written"] > 0
    with database.session() as session:
        assert session.query(DailyAggregate).count() > 0


def test_rebuilding_twice_is_idempotent(client, admin_headers, seeded, database):
    """The property that makes the job safe to re-run after a replay."""
    first = client.post("/analytics/aggregates/rebuild", headers=admin_headers).json()
    with database.session() as session:
        count_after_first = session.query(DailyAggregate).count()

    second = client.post("/analytics/aggregates/rebuild", headers=admin_headers).json()
    with database.session() as session:
        count_after_second = session.query(DailyAggregate).count()

    assert first["rows_written"] == second["rows_written"]
    assert count_after_first == count_after_second


def test_aggregate_totals_match_the_underlying_facts(client, admin_headers, seeded, database):
    client.post("/analytics/aggregates/rebuild", headers=admin_headers)

    with database.session() as session:
        fact_units = sum(f.quantity for f in session.query(SalesFact).all())
        aggregate_units = sum(a.units_sold for a in session.query(DailyAggregate).all())

    assert fact_units == aggregate_units


def test_rebuilding_aggregates_requires_admin(client, staff_headers):
    assert client.post("/analytics/aggregates/rebuild", headers=staff_headers).status_code == 403


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------
def test_replenishment_reports_degradation_when_the_model_is_unavailable(
    client, staff_headers, seeded
):
    """An empty list must never be mistaken for 'nothing to reorder'."""
    body = client.get("/analytics/replenishment", headers=staff_headers).json()

    assert body["items"] == []
    assert body["degraded_reason"] is not None
    assert "unavailable" in body["degraded_reason"].lower()


def test_replenishment_uses_forecasts_when_available(client, staff_headers, seeded):
    from app.deps import get_forecast_client
    from app.main import app as fastapi_app

    class StubForecast:
        def replenishment(self, product_id, store_id, current_stock, threshold):  # noqa: ARG002
            return {
                "product_id": product_id,
                "store_id": store_id,
                "current_stock": current_stock,
                "predicted_demand_7d": 40,
                "recommended_order_quantity": 48,
                "urgency": "CRITICAL",
                "days_of_cover": 0.0,
                "model_version": "v1",
            }

    fastapi_app.dependency_overrides[get_forecast_client] = StubForecast
    try:
        body = client.get("/analytics/replenishment", headers=staff_headers).json()
    finally:
        fastapi_app.dependency_overrides.pop(get_forecast_client, None)

    assert body["degraded_reason"] is None
    assert body["model_version"] == "v1"
    assert body["items"][0]["urgency"] == "CRITICAL"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def test_analytics_requires_staff(client, customer_headers):
    assert client.get("/analytics/dashboard", headers=customer_headers).status_code == 403


def test_analytics_requires_authentication(client):
    assert client.get("/analytics/dashboard").status_code == 401


def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "analytics-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/analytics/dashboard" in paths
    assert "/analytics/replenishment" in paths
