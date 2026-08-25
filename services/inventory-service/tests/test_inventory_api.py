"""API-level tests for the inventory service."""

from __future__ import annotations

import uuid


def _location(client, headers, code="BLR01"):
    return client.post(
        "/locations",
        json={"code": code, "name": f"Store {code}", "city": "Bangalore"},
        headers=headers,
    ).json()


def _restock(client, headers, product_id, location_id, quantity=10, threshold=3):
    return client.post(
        "/inventory/restock",
        json={
            "product_id": str(product_id),
            "location_id": str(location_id),
            "quantity": quantity,
            "reorder_threshold": threshold,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def test_create_location(client, operator_headers):
    response = client.post(
        "/locations",
        json={"code": "MAA01", "name": "Chennai Store", "city": "Chennai"},
        headers=operator_headers,
    )
    assert response.status_code == 201
    assert response.json()["code"] == "MAA01"


def test_duplicate_location_code_returns_409(client, operator_headers):
    _location(client, operator_headers)
    response = client.post(
        "/locations",
        json={"code": "BLR01", "name": "Duplicate", "city": "Bangalore"},
        headers=operator_headers,
    )
    assert response.status_code == 409


def test_malformed_location_code_returns_422(client, operator_headers):
    response = client.post(
        "/locations",
        json={"code": "bad code!", "name": "X", "city": "Y"},
        headers=operator_headers,
    )
    assert response.status_code == 422


def test_customer_cannot_create_a_location(client, customer_headers):
    response = client.post(
        "/locations",
        json={"code": "HYD01", "name": "Hyderabad Store", "city": "Hyderabad"},
        headers=customer_headers,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Restock
# ---------------------------------------------------------------------------
def test_restock_creates_inventory(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()

    response = _restock(client, operator_headers, product_id, loc["location_id"], 25)

    assert response.status_code == 200
    body = response.json()
    assert body["available_quantity"] == 25
    assert body["reserved_quantity"] == 0
    assert body["total_quantity"] == 25


def test_restock_at_unknown_location_returns_404(client, operator_headers):
    response = _restock(client, operator_headers, uuid.uuid4(), uuid.uuid4())
    assert response.status_code == 404


def test_restock_requires_authentication(client):
    response = _restock(client, {}, uuid.uuid4(), uuid.uuid4())
    assert response.status_code == 401


def test_negative_restock_quantity_returns_422(client, operator_headers):
    loc = _location(client, operator_headers)
    response = _restock(client, operator_headers, uuid.uuid4(), loc["location_id"], -5)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_product_inventory_summary_aggregates_locations(client, operator_headers):
    blr = _location(client, operator_headers, "BLR01")
    maa = _location(client, operator_headers, "MAA01")
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, blr["location_id"], 10)
    _restock(client, operator_headers, product_id, maa["location_id"], 15)

    body = client.get(f"/inventory/{product_id}").json()

    assert body["total_available"] == 25
    assert body["locations_in_stock"] == 2
    assert len(body["locations"]) == 2


def test_inventory_for_unstocked_product_is_empty_not_404(client):
    body = client.get(f"/inventory/{uuid.uuid4()}").json()
    assert body["total_available"] == 0
    assert body["locations"] == []


def test_per_location_breakdown(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 7)

    body = client.get(f"/inventory/{product_id}/locations").json()

    assert len(body) == 1
    assert body[0]["location_code"] == "BLR01"
    assert body[0]["available_quantity"] == 7


# ---------------------------------------------------------------------------
# Reserve / release lifecycle over HTTP
# ---------------------------------------------------------------------------
def test_reserve_release_cycle(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 10)
    order_id = str(uuid.uuid4())

    reserved = client.post(
        "/inventory/reserve",
        json={
            "order_id": order_id,
            "lines": [
                {
                    "product_id": str(product_id),
                    "quantity": 4,
                    "location_id": loc["location_id"],
                }
            ],
        },
        headers=operator_headers,
    )
    assert reserved.status_code == 200
    assert reserved.json()["idempotent_replay"] is False
    assert sum(a["quantity"] for a in reserved.json()["allocations"]) == 4

    after_reserve = client.get(f"/inventory/{product_id}").json()
    assert after_reserve["total_available"] == 6
    assert after_reserve["total_reserved"] == 4

    released = client.post(
        "/inventory/release",
        json={"order_id": order_id, "reason": "PAYMENT_FAILED"},
        headers=operator_headers,
    )
    assert released.status_code == 200
    assert released.json()["released_units"] == 4

    after_release = client.get(f"/inventory/{product_id}").json()
    assert after_release["total_available"] == 10
    assert after_release["total_reserved"] == 0


def test_over_reserving_returns_409_with_available_quantity(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 3)

    response = client.post(
        "/inventory/reserve",
        json={
            "order_id": str(uuid.uuid4()),
            "lines": [
                {
                    "product_id": str(product_id),
                    "quantity": 5,
                    "location_id": loc["location_id"],
                }
            ],
        },
        headers=operator_headers,
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "insufficient_inventory"
    assert error["details"]["available"] == 3
    assert error["details"]["requested"] == 5


def test_duplicate_reserve_over_http_is_idempotent(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 10)
    payload = {
        "order_id": str(uuid.uuid4()),
        "lines": [
            {"product_id": str(product_id), "quantity": 3, "location_id": loc["location_id"]}
        ],
    }

    client.post("/inventory/reserve", json=payload, headers=operator_headers)
    second = client.post("/inventory/reserve", json=payload, headers=operator_headers)

    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert client.get(f"/inventory/{product_id}").json()["total_reserved"] == 3


def test_reserve_requires_operator_role(client, customer_headers):
    response = client.post(
        "/inventory/reserve",
        json={
            "order_id": str(uuid.uuid4()),
            "lines": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
        },
        headers=customer_headers,
    )
    assert response.status_code == 403


def test_commit_over_http_consumes_stock(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 10)
    order_id = str(uuid.uuid4())
    client.post(
        "/inventory/reserve",
        json={
            "order_id": order_id,
            "lines": [
                {"product_id": str(product_id), "quantity": 4, "location_id": loc["location_id"]}
            ],
        },
        headers=operator_headers,
    )

    response = client.post(
        "/inventory/commit", json={"order_id": order_id}, headers=operator_headers
    )

    assert response.status_code == 200
    summary = client.get(f"/inventory/{product_id}").json()
    assert summary["total_available"] == 6
    assert summary["total_reserved"] == 0


def test_release_of_unknown_order_returns_404(client, operator_headers):
    response = client.post(
        "/inventory/release",
        json={"order_id": str(uuid.uuid4())},
        headers=operator_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Low stock and movements
# ---------------------------------------------------------------------------
def test_low_stock_endpoint_reports_shortfall(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 2, threshold=10)

    body = client.get("/inventory/low-stock", headers=operator_headers).json()

    assert len(body) == 1
    assert body[0]["shortfall"] == 8


def test_movements_endpoint_returns_the_ledger(client, operator_headers):
    loc = _location(client, operator_headers)
    product_id = uuid.uuid4()
    _restock(client, operator_headers, product_id, loc["location_id"], 10)

    body = client.get(f"/inventory/{product_id}/movements", headers=operator_headers).json()

    assert len(body) == 1
    assert body[0]["movement_type"] == "RESTOCK"


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
def test_health_and_docs(client):
    assert client.get("/health").json()["service"] == "inventory-service"
    schema = client.get("/openapi.json").json()
    assert "/inventory/reserve" in schema["paths"]
    assert "/inventory/release" in schema["paths"]
