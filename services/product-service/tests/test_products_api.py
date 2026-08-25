"""API-level tests for the product catalog.

These assert on the contract other services and the frontend depend on:
status codes, the error envelope, pagination shape, and the authorization
boundary.
"""

from __future__ import annotations

import uuid


def _create(client, headers, **overrides):
    payload = {
        "sku": "SKU-BASE-001",
        "name": "Base product",
        "category": "Electronics",
        "brand": "Acme",
        "price": "1999.00",
        "currency": "INR",
        "weight_grams": 500,
    }
    payload.update(overrides)
    return client.post("/products", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def test_create_product_returns_201_and_generated_uuid(client, admin_headers, sample_product):
    response = client.post("/products", json=sample_product, headers=admin_headers)

    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["product_id"])  # a real UUID, not a sequential int
    assert body["sku"] == "TV-SAM-55U8"
    assert body["category"] == "Electronics"
    assert body["status"] == "ACTIVE"
    assert body["price"] == "48999.00"


def test_sku_is_uppercased_on_create(client, admin_headers):
    response = _create(client, admin_headers, sku="sku-lower-123")
    assert response.status_code == 201
    assert response.json()["sku"] == "SKU-LOWER-123"


def test_duplicate_sku_returns_409_conflict(client, admin_headers, sample_product):
    assert client.post("/products", json=sample_product, headers=admin_headers).status_code == 201

    duplicate = client.post("/products", json=sample_product, headers=admin_headers)

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


def test_negative_price_is_rejected_with_422(client, admin_headers):
    response = _create(client, admin_headers, price="-5.00")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_malformed_sku_is_rejected(client, admin_headers):
    response = _create(client, admin_headers, sku="a b")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Authorization -- enforced server-side
# ---------------------------------------------------------------------------
def test_create_without_token_returns_401(client, sample_product):
    response = client.post("/products", json=sample_product)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_create_as_customer_returns_403(client, customer_headers, sample_product):
    response = client.post("/products", json=sample_product, headers=customer_headers)
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "forbidden"
    assert body["details"]["your_role"] == "CUSTOMER"
    assert "ADMIN" in body["details"]["required_roles"]


def test_tampered_token_returns_401(client, sample_product):
    headers = {"Authorization": "Bearer not.a.real.token"}
    response = client.post("/products", json=sample_product, headers=headers)
    assert response.status_code == 401


def test_reading_the_catalog_needs_no_token(client):
    assert client.get("/products").status_code == 200


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
def test_get_product_by_id(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()

    response = client.get(f"/products/{created['product_id']}")

    assert response.status_code == 200
    assert response.json()["sku"] == created["sku"]


def test_get_unknown_product_returns_404_envelope(client):
    response = client.get(f"/products/{uuid.uuid4()}")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"]  # correlation ID is always present


def test_get_by_sku(client, admin_headers, sample_product):
    client.post("/products", json=sample_product, headers=admin_headers)
    response = client.get("/products/sku/tv-sam-55u8")
    assert response.status_code == 200
    assert response.json()["sku"] == "TV-SAM-55U8"


def test_bulk_lookup_returns_only_known_ids(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()

    response = client.post(
        "/products/bulk",
        json={"product_ids": [created["product_id"], str(uuid.uuid4())]},
    )

    assert response.status_code == 200
    assert [p["product_id"] for p in response.json()] == [created["product_id"]]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_pagination_splits_results_and_reports_totals(client, admin_headers):
    for i in range(25):
        assert _create(client, admin_headers, sku=f"SKU-PAGE-{i:03d}").status_code == 201

    first = client.get("/products?page=1&page_size=10").json()
    assert len(first["items"]) == 10
    assert first["total"] == 25
    assert first["total_pages"] == 3
    assert first["has_next"] is True
    assert first["has_previous"] is False

    last = client.get("/products?page=3&page_size=10").json()
    assert len(last["items"]) == 5
    assert last["has_next"] is False
    assert last["has_previous"] is True


def test_page_size_is_capped_server_side(client):
    """A client cannot ask for the whole table."""
    response = client.get("/products?page_size=100000")
    assert response.status_code == 422


def test_pages_do_not_overlap(client, admin_headers):
    for i in range(12):
        _create(client, admin_headers, sku=f"SKU-ORDER-{i:03d}")

    page1 = {p["sku"] for p in client.get("/products?page=1&page_size=6").json()["items"]}
    page2 = {p["sku"] for p in client.get("/products?page=2&page_size=6").json()["items"]}

    assert len(page1) == 6
    assert len(page2) == 6
    assert page1.isdisjoint(page2)


# ---------------------------------------------------------------------------
# Search and filtering
# ---------------------------------------------------------------------------
def test_search_matches_name_case_insensitively(client, admin_headers, sample_product):
    client.post("/products", json=sample_product, headers=admin_headers)

    response = client.get("/products/search?q=samsung")

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_search_matches_sku(client, admin_headers, sample_product):
    client.post("/products", json=sample_product, headers=admin_headers)
    assert client.get("/products/search?q=55U8").json()["total"] == 1


def test_search_requires_two_characters(client):
    assert client.get("/products/search?q=a").status_code == 422


def test_search_wildcards_are_escaped_not_interpreted(client, admin_headers):
    """A bare '%' must not match every row."""
    _create(client, admin_headers, sku="SKU-PLAIN-1", name="Plain kettle")
    _create(client, admin_headers, sku="SKU-SALE-1", name="50% off blender")

    result = client.get("/products/search?q=50%25").json()

    assert result["total"] == 1
    assert result["items"][0]["name"] == "50% off blender"


def test_filter_by_category(client, admin_headers):
    _create(client, admin_headers, sku="SKU-EL-1", category="Electronics")
    _create(client, admin_headers, sku="SKU-GR-1", category="Groceries")

    response = client.get("/products/category/electronics")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["sku"] == "SKU-EL-1"


def test_filter_by_brand(client, admin_headers):
    _create(client, admin_headers, sku="SKU-B1", brand="Acme")
    _create(client, admin_headers, sku="SKU-B2", brand="Globex")

    assert client.get("/products?brand=Globex").json()["total"] == 1


# ---------------------------------------------------------------------------
# Update and soft delete
# ---------------------------------------------------------------------------
def test_update_changes_only_supplied_fields(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()

    response = client.put(
        f"/products/{created['product_id']}",
        json={"price": "44999.00"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["price"] == "44999.00"
    assert body["name"] == created["name"]  # untouched
    assert body["sku"] == created["sku"]


def test_update_can_move_product_to_a_new_category(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()

    response = client.put(
        f"/products/{created['product_id']}",
        json={"category": "Home Appliances"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["category"] == "Home Appliances"


def test_update_unknown_product_returns_404(client, admin_headers):
    response = client.put(
        f"/products/{uuid.uuid4()}", json={"price": "10.00"}, headers=admin_headers
    )
    assert response.status_code == 404


def test_delete_is_a_soft_delete(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()

    response = client.delete(f"/products/{created['product_id']}", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "DISCONTINUED"
    # The row still exists -- order history must keep resolving.
    assert client.get(f"/products/{created['product_id']}").status_code == 200


def test_discontinued_products_are_hidden_from_the_catalog(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()
    client.delete(f"/products/{created['product_id']}", headers=admin_headers)

    assert client.get("/products").json()["total"] == 0
    assert client.get("/products?include_discontinued=true").json()["total"] == 1


def test_double_delete_returns_409(client, admin_headers, sample_product):
    created = client.post("/products", json=sample_product, headers=admin_headers).json()
    client.delete(f"/products/{created['product_id']}", headers=admin_headers)

    second = client.delete(f"/products/{created['product_id']}", headers=admin_headers)

    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
def test_category_is_created_on_first_product_and_reused(client, admin_headers):
    _create(client, admin_headers, sku="SKU-C1", category="Electronics")
    _create(client, admin_headers, sku="SKU-C2", category="electronics")

    categories = client.get("/categories").json()

    assert len([c for c in categories if c["slug"] == "electronics"]) == 1


# ---------------------------------------------------------------------------
# Platform endpoints
# ---------------------------------------------------------------------------
def test_health_probe_does_not_depend_on_the_database(client):
    body = client.get("/health").json()
    assert body["status"] == "alive"
    assert body["service"] == "product-service"


def test_metrics_endpoint_exposes_prometheus_counters(client):
    client.get("/products")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_every_response_carries_a_request_id(client):
    response = client.get("/products")
    assert response.headers["X-Request-ID"]


def test_inbound_request_id_is_propagated(client):
    response = client.get("/products", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["X-Request-ID"] == "trace-me-123"


def test_openapi_documents_every_route(client):
    schema = client.get("/openapi.json").json()
    for path in ("/products", "/products/{product_id}", "/products/search"):
        assert path in schema["paths"], f"{path} missing from OpenAPI schema"
