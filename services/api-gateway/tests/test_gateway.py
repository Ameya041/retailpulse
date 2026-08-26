"""API gateway tests."""

from __future__ import annotations

import httpx
import pytest

from app.routing import BreakerState, resolve


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "expected_backend", "expected_upstream"),
    [
        ("/api/products", "product-service", "/products"),
        ("/api/products/123", "product-service", "/products/123"),
        ("/api/products/search", "product-service", "/products/search"),
        ("/api/categories", "product-service", "/categories"),
        ("/api/inventory/abc/locations", "inventory-service", "/inventory/abc/locations"),
        ("/api/orders", "order-service", "/orders"),
        ("/api/cart/items", "order-service", "/cart/items"),
        ("/api/auth/login", "user-service", "/auth/login"),
        ("/api/payments/abc/refund", "payment-service", "/payments/abc/refund"),
        ("/api/fulfilment/abc/ship", "fulfilment-service", "/fulfilment/abc/ship"),
        ("/api/analytics/dashboard", "analytics-service", "/analytics/dashboard"),
        ("/api/forecast/products", "ml-service", "/forecast/products"),
        # Model metadata is public while forecasting is staff-only, so it gets
        # its own prefix rather than nesting under /forecast.
        ("/api/model/info", "ml-service", "/model/info"),
    ],
)
def test_paths_route_to_the_owning_service(path, expected_backend, expected_upstream):
    backend, upstream_path = resolve(path)
    assert backend.name == expected_backend
    assert upstream_path == expected_upstream


def test_unknown_path_does_not_resolve():
    assert resolve("/api/nonsense") is None


def test_non_api_path_does_not_resolve():
    assert resolve("/products") is None


def test_longest_prefix_wins():
    """/api/products must not swallow a sibling prefix."""
    backend, _ = resolve("/api/products/search")
    assert backend.prefix == "/api/products"


# ---------------------------------------------------------------------------
# Proxying
# ---------------------------------------------------------------------------
def test_request_is_forwarded_to_the_right_upstream(client, upstream):
    upstream.json_body = {"items": [], "total": 0}

    response = client.get("/api/products")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}
    assert upstream.last.url.path == "/products"
    assert str(upstream.last.url).startswith("http://localhost:8001")


def test_query_parameters_are_preserved(client, upstream):
    client.get("/api/products?page=2&page_size=5&q=widget")
    assert dict(upstream.last.url.params) == {"page": "2", "page_size": "5", "q": "widget"}


def test_request_body_is_forwarded(client, upstream, customer_headers):
    client.post("/api/orders", json={"shipping_address": "somewhere"}, headers=customer_headers)
    assert b"shipping_address" in upstream.last.content


def test_authorization_header_is_passed_through(client, upstream, customer_headers):
    """The gateway verifies the token but each service enforces its own rules."""
    client.get("/api/orders", headers=customer_headers)
    assert upstream.last.headers["authorization"] == customer_headers["Authorization"]


def test_upstream_status_code_is_preserved(client, upstream):
    upstream.status_code = 404
    assert client.get("/api/products/missing").status_code == 404


def test_upstream_error_body_is_preserved(client, upstream):
    upstream.status_code = 409
    upstream.json_body = {"error": {"code": "conflict", "message": "already exists"}}

    response = client.post("/api/products", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_every_proxied_method_is_supported(client, upstream, method):
    response = client.request(method, "/api/products/abc")
    assert response.status_code == 200
    assert upstream.last.method == method


def test_unknown_route_returns_404_without_calling_any_backend(client, upstream):
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert upstream.requests == []


def test_backend_name_is_reported_on_the_response(client, upstream):
    """Makes it obvious which service answered when debugging."""
    response = client.get("/api/products")
    assert response.headers["X-Gateway-Backend"] == "product-service"


# ---------------------------------------------------------------------------
# Header hygiene
# ---------------------------------------------------------------------------
def test_hop_by_hop_headers_are_stripped():
    """Forwarding these produces corrupted responses that are hard to trace.

    Tested against the filter directly: the HTTP client legitimately sets its
    own Connection and Host on the outgoing request, so their mere presence
    downstream proves nothing -- what matters is that *our* values were dropped.
    """
    from app.proxy import _forwardable

    forwarded = _forwardable(
        {
            "Connection": "close",
            "Host": "gateway.local",
            "Content-Length": "999",
            "Transfer-Encoding": "chunked",
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
        },
        {},
    )

    assert "connection" not in forwarded
    assert "host" not in forwarded
    assert "content-length" not in forwarded
    assert "transfer-encoding" not in forwarded
    # End-to-end headers must survive.
    assert forwarded["authorization"] == "Bearer token"
    assert forwarded["content-type"] == "application/json"


def test_overrides_replace_rather_than_duplicate_a_header():
    """Header names are case-insensitive; a case mismatch used to send both."""
    from app.proxy import _forwardable

    forwarded = _forwardable({"x-request-id": "original"}, {"X-Request-ID": "override"})

    assert forwarded == {"x-request-id": "override"}


def test_empty_override_values_are_dropped():
    """Some servers treat a blank header as malformed rather than absent."""
    from app.proxy import _forwardable

    assert "x-forwarded-for" not in _forwardable({}, {"X-Forwarded-For": ""})


def test_our_host_header_is_not_forwarded_upstream(client, upstream):
    client.get("/api/products", headers={"Host": "gateway.local"})
    assert upstream.last.headers.get("host") != "gateway.local"


def test_request_id_is_propagated_to_the_upstream(client, upstream):
    """One ID threads through the gateway and every service it touches."""
    client.get("/api/products", headers={"X-Request-ID": "trace-me-123"})
    assert upstream.last.headers["x-request-id"] == "trace-me-123"


def test_request_id_is_generated_when_absent(client, upstream):
    client.get("/api/products")
    assert upstream.last.headers["x-request-id"]


def test_forwarded_for_is_set(client, upstream):
    client.get("/api/products")
    assert "x-forwarded-for" in {k.lower() for k in upstream.last.headers}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_requests_under_the_limit_pass_through(client, upstream, customer_headers):
    for _ in range(10):
        assert client.get("/api/products", headers=customer_headers).status_code == 200


def test_exceeding_the_limit_returns_429(client, customer_headers, limiter):
    # Burn the authenticated allowance (100/min by default).
    for _ in range(100):
        client.get("/api/products", headers=customer_headers)

    response = client.get("/api/products", headers=customer_headers)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


def test_429_carries_retry_after(client, customer_headers):
    for _ in range(100):
        client.get("/api/products", headers=customer_headers)

    response = client.get("/api/products", headers=customer_headers)

    assert int(response.json()["error"]["details"]["retry_after_seconds"]) > 0


def test_rate_limit_headers_are_sent_on_successful_requests(client, customer_headers):
    response = client.get("/api/products", headers=customer_headers)

    assert response.headers["X-RateLimit-Limit"] == "100"
    assert int(response.headers["X-RateLimit-Remaining"]) < 100


def test_anonymous_callers_get_a_smaller_allowance(client):
    """An IP is a weak identity, so it earns less trust than a login."""
    response = client.get("/api/products")
    assert response.headers["X-RateLimit-Limit"] == "30"


def test_anonymous_limit_is_enforced(client):
    for _ in range(30):
        client.get("/api/products")

    assert client.get("/api/products").status_code == 429


def test_two_users_are_limited_independently(client, customer_headers, admin_headers):
    for _ in range(100):
        client.get("/api/products", headers=customer_headers)

    assert client.get("/api/products", headers=customer_headers).status_code == 429
    assert client.get("/api/products", headers=admin_headers).status_code == 200


def test_a_rate_limited_request_never_reaches_the_backend(client, upstream, customer_headers):
    """The point of limiting at the edge."""
    for _ in range(100):
        client.get("/api/products", headers=customer_headers)
    before = len(upstream.requests)

    client.get("/api/products", headers=customer_headers)

    assert len(upstream.requests) == before


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def test_upstream_timeout_returns_503(client, upstream):
    upstream.raise_exc = httpx.TimeoutException("too slow")

    response = client.get("/api/products")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_unreachable_upstream_returns_503(client, upstream):
    upstream.raise_exc = httpx.ConnectError("connection refused")
    assert client.get("/api/products").status_code == 503


def test_repeated_failures_open_the_circuit(client, upstream, breakers):
    upstream.raise_exc = httpx.ConnectError("down")

    for _ in range(3):  # threshold is 3 in tests
        client.get("/api/products")

    assert breakers.get("product-service").state is BreakerState.OPEN


def test_an_open_circuit_fails_fast_without_calling_the_backend(client, upstream, breakers):
    """This is what stops one sick service exhausting the gateway."""
    upstream.raise_exc = httpx.ConnectError("down")
    for _ in range(3):
        client.get("/api/products")
    calls_before = len(upstream.requests)

    response = client.get("/api/products")

    assert response.status_code == 503
    assert len(upstream.requests) == calls_before, "open circuit still called the backend"
    assert response.json()["error"]["details"]["circuit"] == "OPEN"


def test_a_broken_backend_does_not_affect_a_healthy_one(client, upstream, breakers):
    """Failure isolation: the whole reason for per-backend breakers."""
    upstream.raise_exc = httpx.ConnectError("down")
    for _ in range(3):
        client.get("/api/products")
    assert breakers.get("product-service").state is BreakerState.OPEN

    upstream.raise_exc = None
    assert client.get("/api/orders").status_code == 200
    assert breakers.get("order-service").state is BreakerState.CLOSED


def test_5xx_from_the_backend_counts_as_a_failure(client, upstream, breakers):
    upstream.status_code = 500

    for _ in range(3):
        client.get("/api/products")

    assert breakers.get("product-service").state is BreakerState.OPEN


def test_4xx_from_the_backend_does_not_open_the_circuit(client, upstream, breakers):
    """A 404 means the backend is healthy and doing its job."""
    upstream.status_code = 404

    for _ in range(10):
        client.get("/api/products/missing")

    assert breakers.get("product-service").state is BreakerState.CLOSED


def test_a_success_resets_the_failure_count(client, upstream, breakers):
    upstream.raise_exc = httpx.ConnectError("blip")
    client.get("/api/products")
    client.get("/api/products")

    upstream.raise_exc = None
    client.get("/api/products")

    assert breakers.get("product-service").consecutive_failures == 0
    assert breakers.get("product-service").state is BreakerState.CLOSED


def test_circuit_recovers_through_half_open(client, upstream, breakers):
    upstream.raise_exc = httpx.ConnectError("down")
    for _ in range(3):
        client.get("/api/products")
    breaker = breakers.get("product-service")
    assert breaker.state is BreakerState.OPEN

    # Simulate the cool-down elapsing.
    breaker.opened_at -= breaker.cool_down_seconds + 1
    upstream.raise_exc = None

    assert client.get("/api/products").status_code == 200
    assert breaker.state is BreakerState.CLOSED


def test_a_failed_probe_reopens_the_circuit(client, upstream, breakers):
    upstream.raise_exc = httpx.ConnectError("down")
    for _ in range(3):
        client.get("/api/products")
    breaker = breakers.get("product-service")
    breaker.opened_at -= breaker.cool_down_seconds + 1

    client.get("/api/products")  # probe, still failing

    assert breaker.state is BreakerState.OPEN


# ---------------------------------------------------------------------------
# Gateway's own endpoints
# ---------------------------------------------------------------------------
def test_route_table_is_published(client):
    body = client.get("/gateway/routes").json()
    prefixes = {r["prefix"] for r in body["routes"]}
    assert "/api/products" in prefixes
    assert "/api/orders" in prefixes


def test_circuit_states_require_admin(client, customer_headers):
    assert client.get("/gateway/circuits", headers=customer_headers).status_code == 403


def test_admin_can_read_circuit_states(client, admin_headers, upstream):
    client.get("/api/products")
    body = client.get("/gateway/circuits", headers=admin_headers).json()
    assert any(c["backend"] == "product-service" for c in body["circuits"])


def test_health_does_not_depend_on_any_backend(client, upstream):
    upstream.raise_exc = httpx.ConnectError("everything is down")
    assert client.get("/health").json()["status"] == "alive"


def test_metrics_are_exposed(client):
    client.get("/api/products")
    assert "http_requests_total" in client.get("/metrics").text
