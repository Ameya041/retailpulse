"""Cart and order API tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

from tests.conftest import (
    DISCONTINUED_ID,
    SHIPPING_ADDRESS,
    USD_ID,
    WIDGET_ID,
    advance,
)


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------
def test_new_cart_starts_empty(client, customer_headers):
    body = client.get("/cart", headers=customer_headers).json()
    assert body["items"] == []
    assert body["item_count"] == 0
    assert Decimal(body["total_amount"]) == Decimal("0.00")


def test_add_item_prices_the_line(client, customer_headers):
    body = client.post(
        "/cart/items",
        json={"product_id": str(WIDGET_ID), "quantity": 3},
        headers=customer_headers,
    ).json()

    assert body["item_count"] == 3
    assert Decimal(body["total_amount"]) == Decimal("599.97")  # 199.99 x 3
    assert body["items"][0]["sku"] == "WIDGET-001"


def test_adding_the_same_product_increments_quantity(client, customer_headers):
    client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 2}, headers=customer_headers
    )
    body = client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 3}, headers=customer_headers
    ).json()

    assert len(body["items"]) == 1  # one line, not two
    assert body["items"][0]["quantity"] == 5


def test_add_unknown_product_returns_404(client, customer_headers):
    response = client.post(
        "/cart/items",
        json={"product_id": str(uuid.uuid4()), "quantity": 1},
        headers=customer_headers,
    )
    assert response.status_code == 404


def test_add_discontinued_product_returns_409(client, customer_headers):
    response = client.post(
        "/cart/items",
        json={"product_id": str(DISCONTINUED_ID), "quantity": 1},
        headers=customer_headers,
    )
    assert response.status_code == 409


def test_setting_quantity_to_zero_removes_the_line(client, customer_headers):
    client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 2}, headers=customer_headers
    )

    body = client.put(
        f"/cart/items/{WIDGET_ID}", json={"quantity": 0}, headers=customer_headers
    ).json()

    assert body["items"] == []


def test_update_item_not_in_cart_returns_404(client, customer_headers):
    response = client.put(
        f"/cart/items/{WIDGET_ID}", json={"quantity": 2}, headers=customer_headers
    )
    assert response.status_code == 404


def test_clear_empties_the_cart(client, customer_headers):
    client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 2}, headers=customer_headers
    )
    assert client.delete("/cart", headers=customer_headers).json()["items"] == []


def test_carts_are_isolated_between_customers(client, customer_headers, other_customer_headers):
    client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 2}, headers=customer_headers
    )

    assert client.get("/cart", headers=other_customer_headers).json()["items"] == []


def test_cart_requires_authentication(client):
    assert client.get("/cart").status_code == 401


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------
def test_create_order_from_explicit_lines(client, customer_headers):
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 2}],
        },
        headers=customer_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "CREATED"
    assert Decimal(body["total_amount"]) == Decimal("399.98")
    assert body["items"][0]["unit_price"] == "199.99"


def test_order_total_equals_the_sum_of_its_lines(client, placed_order):
    line_total = sum(Decimal(i["subtotal"]) for i in placed_order["items"])
    assert Decimal(placed_order["total_amount"]) == line_total
    # 199.99 x 2 + 1500.50 x 1
    assert Decimal(placed_order["total_amount"]) == Decimal("1900.48")


def test_create_order_from_cart_consumes_the_cart(client, customer_headers):
    client.post(
        "/cart/items", json={"product_id": str(WIDGET_ID), "quantity": 2}, headers=customer_headers
    )

    response = client.post(
        "/orders", json={"shipping_address": SHIPPING_ADDRESS}, headers=customer_headers
    )

    assert response.status_code == 201
    assert client.get("/cart", headers=customer_headers).json()["items"] == []


def test_ordering_an_empty_cart_returns_400(client, customer_headers):
    response = client.post(
        "/orders", json={"shipping_address": SHIPPING_ADDRESS}, headers=customer_headers
    )
    assert response.status_code == 400


def test_order_with_unknown_product_returns_400(client, customer_headers):
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
        },
        headers=customer_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["unknown_product_ids"]


def test_order_with_discontinued_product_returns_409(client, customer_headers):
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(DISCONTINUED_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )
    assert response.status_code == 409
    assert "OLD-001" in response.json()["error"]["details"]["unavailable_skus"]


def test_mixed_currency_order_is_rejected(client, customer_headers):
    """Summing INR and USD into one total would be meaningless."""
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [
                {"product_id": str(WIDGET_ID), "quantity": 1},
                {"product_id": str(USD_ID), "quantity": 1},
            ],
        },
        headers=customer_headers,
    )
    assert response.status_code == 400
    assert set(response.json()["error"]["details"]["currencies"]) == {"INR", "USD"}


def test_duplicate_lines_are_rejected(client, customer_headers):
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [
                {"product_id": str(WIDGET_ID), "quantity": 1},
                {"product_id": str(WIDGET_ID), "quantity": 2},
            ],
        },
        headers=customer_headers,
    )
    assert response.status_code == 422


def test_short_shipping_address_is_rejected(client, customer_headers):
    response = client.post(
        "/orders",
        json={
            "shipping_address": "x",
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )
    assert response.status_code == 422


def test_order_price_is_frozen_against_later_catalog_changes(client, customer_headers, catalog, placed_order):
    """The whole reason unit_price is snapshotted onto the order line."""
    from app.product_client import CatalogProduct

    catalog.add(
        CatalogProduct(
            product_id=WIDGET_ID,
            sku="WIDGET-001",
            name="Standard Widget",
            price=Decimal("999.99"),  # price hike after the order was placed
            currency="INR",
            status="ACTIVE",
        )
    )

    refetched = client.get(f"/orders/{placed_order['order_id']}", headers=customer_headers).json()

    assert Decimal(refetched["total_amount"]) == Decimal("1900.48")
    widget_line = next(i for i in refetched["items"] if i["product_id"] == str(WIDGET_ID))
    assert widget_line["unit_price"] == "199.99"


def test_creating_an_order_requires_authentication(client):
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
    )
    assert response.status_code == 401


def test_catalog_outage_fails_the_order_rather_than_guessing(client, customer_headers):
    """Fail closed: never invent a price when the catalog is unreachable."""
    from app.deps import get_catalog
    from app.main import app as fastapi_app
    from retailpulse_common.errors import ServiceUnavailableError

    class DeadCatalog:
        def get_many(self, product_ids):
            raise ServiceUnavailableError("The product catalog is unavailable.")

    fastapi_app.dependency_overrides[get_catalog] = DeadCatalog
    try:
        response = client.post(
            "/orders",
            json={
                "shipping_address": SHIPPING_ADDRESS,
                "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
            },
            headers=customer_headers,
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_catalog, None)

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Reading orders / ownership
# ---------------------------------------------------------------------------
def test_customer_sees_only_their_own_orders(client, customer_headers, other_customer_headers, placed_order):
    mine = client.get("/orders", headers=customer_headers).json()
    theirs = client.get("/orders", headers=other_customer_headers).json()

    assert mine["total"] == 1
    assert theirs["total"] == 0


def test_another_customers_order_returns_404_not_403(client, other_customer_headers, placed_order):
    """403 would confirm the order exists. 404 leaks nothing."""
    response = client.get(f"/orders/{placed_order['order_id']}", headers=other_customer_headers)
    assert response.status_code == 404


def test_staff_can_read_any_order(client, staff_headers, placed_order):
    response = client.get(f"/orders/{placed_order['order_id']}", headers=staff_headers)
    assert response.status_code == 200


def test_order_detail_includes_history_and_next_states(client, customer_headers, placed_order):
    body = client.get(f"/orders/{placed_order['order_id']}", headers=customer_headers).json()

    assert body["transitions"][0]["to_status"] == "CREATED"
    assert set(body["allowed_next_statuses"]) == {"INVENTORY_RESERVED", "CANCELLED"}


def test_customer_cannot_list_all_orders(client, customer_headers):
    assert client.get("/orders/all", headers=customer_headers).status_code == 403


def test_staff_can_filter_all_orders_by_status(client, staff_headers, placed_order):
    body = client.get("/orders/all?status=CREATED", headers=staff_headers).json()
    assert body["total"] == 1
    assert client.get("/orders/all?status=DELIVERED", headers=staff_headers).json()["total"] == 0


def test_unknown_order_returns_404(client, customer_headers):
    assert client.get(f"/orders/{uuid.uuid4()}", headers=customer_headers).status_code == 404


# ---------------------------------------------------------------------------
# Transitions over HTTP
# ---------------------------------------------------------------------------
def test_full_happy_path_over_http(client, staff_headers, customer_headers, placed_order):
    order_id = placed_order["order_id"]

    advance(
        client, staff_headers, order_id,
        "INVENTORY_RESERVED", "PAYMENT_CONFIRMED", "CONFIRMED",
        "FULFILMENT_STARTED", "SHIPPED", "DELIVERED",
    )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    assert body["status"] == "DELIVERED"
    assert body["allowed_next_statuses"] == []
    assert [t["to_status"] for t in body["transitions"]][-1] == "DELIVERED"


def test_illegal_transition_returns_409_and_says_what_is_allowed(client, staff_headers, placed_order):
    response = client.patch(
        f"/orders/{placed_order['order_id']}/status",
        json={"status": "DELIVERED"},
        headers=staff_headers,
    )

    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details["current_status"] == "CREATED"
    assert "INVENTORY_RESERVED" in details["allowed_next"]


def test_delivered_order_cannot_be_moved_back(client, staff_headers, placed_order):
    order_id = placed_order["order_id"]
    advance(
        client, staff_headers, order_id,
        "INVENTORY_RESERVED", "PAYMENT_CONFIRMED", "CONFIRMED",
        "FULFILMENT_STARTED", "SHIPPED", "DELIVERED",
    )

    response = client.patch(
        f"/orders/{order_id}/status", json={"status": "CREATED"}, headers=staff_headers
    )

    assert response.status_code == 409


def test_repeating_a_transition_is_an_idempotent_no_op(client, staff_headers, placed_order):
    """Kafka redelivery must not append duplicate history rows."""
    order_id = placed_order["order_id"]
    advance(client, staff_headers, order_id, "INVENTORY_RESERVED")

    second = client.patch(
        f"/orders/{order_id}/status",
        json={"status": "INVENTORY_RESERVED"},
        headers=staff_headers,
    )

    assert second.status_code == 200
    detail = client.get(f"/orders/{order_id}", headers=staff_headers).json()
    reserved_rows = [t for t in detail["transitions"] if t["to_status"] == "INVENTORY_RESERVED"]
    assert len(reserved_rows) == 1


def test_payment_failure_compensation_path(client, staff_headers, placed_order):
    order_id = placed_order["order_id"]

    advance(
        client, staff_headers, order_id,
        "INVENTORY_RESERVED", "PAYMENT_FAILED", "INVENTORY_RELEASED", "CANCELLED",
    )

    body = client.get(f"/orders/{order_id}", headers=staff_headers).json()
    assert body["status"] == "CANCELLED"


def test_cancelling_without_releasing_inventory_is_blocked(client, staff_headers, placed_order):
    """Skipping INVENTORY_RELEASED would strand held stock."""
    order_id = placed_order["order_id"]
    advance(client, staff_headers, order_id, "INVENTORY_RESERVED", "PAYMENT_FAILED")

    response = client.patch(
        f"/orders/{order_id}/status", json={"status": "CANCELLED"}, headers=staff_headers
    )

    assert response.status_code == 409


def test_customer_cannot_drive_the_status_machine(client, customer_headers, placed_order):
    response = client.patch(
        f"/orders/{placed_order['order_id']}/status",
        json={"status": "DELIVERED"},
        headers=customer_headers,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_customer_can_cancel_a_new_order(client, customer_headers, placed_order):
    response = client.post(
        f"/orders/{placed_order['order_id']}/cancel",
        json={"reason": "CHANGED_MIND"},
        headers=customer_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancellation_reason"] == "CHANGED_MIND"


def test_customer_cannot_cancel_once_fulfilment_started(client, customer_headers, staff_headers, placed_order):
    order_id = placed_order["order_id"]
    advance(
        client, staff_headers, order_id,
        "INVENTORY_RESERVED", "PAYMENT_CONFIRMED", "CONFIRMED", "FULFILMENT_STARTED",
    )

    response = client.post(
        f"/orders/{order_id}/cancel", json={"reason": "TOO_LATE"}, headers=customer_headers
    )

    assert response.status_code == 403


def test_cancelling_an_already_cancelled_order_returns_409(client, customer_headers, placed_order):
    order_id = placed_order["order_id"]
    client.post(f"/orders/{order_id}/cancel", json={"reason": "FIRST"}, headers=customer_headers)

    second = client.post(
        f"/orders/{order_id}/cancel", json={"reason": "SECOND"}, headers=customer_headers
    )

    assert second.status_code == 409


def test_customer_cannot_cancel_someone_elses_order(client, other_customer_headers, placed_order):
    response = client.post(
        f"/orders/{placed_order['order_id']}/cancel",
        json={"reason": "NOT_MINE"},
        headers=other_customer_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Pagination and platform
# ---------------------------------------------------------------------------
def test_order_list_is_paginated(client, customer_headers):
    for _ in range(5):
        client.post(
            "/orders",
            json={
                "shipping_address": SHIPPING_ADDRESS,
                "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
            },
            headers=customer_headers,
        )

    page = client.get("/orders?page=1&page_size=2", headers=customer_headers).json()

    assert len(page["items"]) == 2
    assert page["total"] == 5
    assert page["total_pages"] == 3


def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "order-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/orders" in paths
    assert "/cart/items" in paths
