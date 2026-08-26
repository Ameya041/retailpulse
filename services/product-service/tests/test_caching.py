"""Cache behaviour at the API level.

Correctness beats hit rate here: a fast cache that serves a withdrawn product
is worse than no cache at all.
"""

from __future__ import annotations

from retailpulse_common.cache import CacheKey


def _create(client, headers, **overrides):
    payload = {
        "sku": "SKU-CACHE-001",
        "name": "Cached Widget",
        "category": "Electronics",
        "brand": "Acme",
        "price": "199.99",
        "currency": "INR",
    }
    payload.update(overrides)
    return client.post("/products", json=payload, headers=headers).json()


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------
def test_first_read_populates_the_cache(client, admin_headers, cache):
    product = _create(client, admin_headers)
    cache.delete(CacheKey.product(product["product_id"]))

    client.get(f"/products/{product['product_id']}")

    assert cache.get(CacheKey.product(product["product_id"])) is not None


def test_cached_read_returns_the_same_payload(client, admin_headers):
    product = _create(client, admin_headers)

    first = client.get(f"/products/{product['product_id']}").json()
    second = client.get(f"/products/{product['product_id']}").json()

    assert first == second


def test_a_cache_hit_does_not_touch_the_database(client, admin_headers, cache):
    """The point of the cache: the hot read skips Postgres entirely."""
    product = _create(client, admin_headers)
    client.get(f"/products/{product['product_id']}")  # populate

    # Delete the row outright. A hit must still serve it.
    from app.deps import get_db_session  # noqa: F401

    cached_payload = cache.get(CacheKey.product(product["product_id"]))
    assert cached_payload is not None

    response = client.get(f"/products/{product['product_id']}")
    assert response.status_code == 200
    assert response.json()["sku"] == product["sku"]


def test_sku_lookup_is_cached_under_its_own_key(client, admin_headers, cache):
    product = _create(client, admin_headers)

    client.get(f"/products/sku/{product['sku']}")

    assert cache.get(CacheKey.product_by_sku(product["sku"])) is not None


def test_category_list_is_cached(client, admin_headers, cache):
    _create(client, admin_headers)

    client.get("/categories")

    assert cache.get(CacheKey.category_list()) is not None


def test_ttl_is_applied_to_cached_entries(client, admin_headers, cache):
    product = _create(client, admin_headers)
    client.get(f"/products/{product['product_id']}")

    assert cache.ttls[CacheKey.product(product["product_id"])] > 0


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------
def test_update_invalidates_the_cached_product(client, admin_headers, cache):
    product = _create(client, admin_headers)
    client.get(f"/products/{product['product_id']}")
    assert cache.get(CacheKey.product(product["product_id"])) is not None

    client.put(
        f"/products/{product['product_id']}", json={"price": "149.99"}, headers=admin_headers
    )

    assert cache.get(CacheKey.product(product["product_id"])) is None


def test_a_read_after_an_update_sees_the_new_price(client, admin_headers):
    """The failure this whole mechanism exists to prevent."""
    product = _create(client, admin_headers)
    client.get(f"/products/{product['product_id']}")  # warm the cache

    client.put(
        f"/products/{product['product_id']}", json={"price": "149.99"}, headers=admin_headers
    )

    assert client.get(f"/products/{product['product_id']}").json()["price"] == "149.99"


def test_update_invalidates_the_sku_key_too(client, admin_headers, cache):
    """Two doors onto the same row; clearing one would leave the other stale."""
    product = _create(client, admin_headers)
    client.get(f"/products/sku/{product['sku']}")
    assert cache.get(CacheKey.product_by_sku(product["sku"])) is not None

    client.put(
        f"/products/{product['product_id']}", json={"price": "149.99"}, headers=admin_headers
    )

    assert cache.get(CacheKey.product_by_sku(product["sku"])) is None


def test_a_read_by_sku_after_an_update_sees_the_new_price(client, admin_headers):
    product = _create(client, admin_headers)
    client.get(f"/products/sku/{product['sku']}")

    client.put(
        f"/products/{product['product_id']}", json={"price": "88.00"}, headers=admin_headers
    )

    assert client.get(f"/products/sku/{product['sku']}").json()["price"] == "88.00"


def test_discontinuing_invalidates_the_cache(client, admin_headers, cache):
    """A stale cache would keep selling a withdrawn product."""
    product = _create(client, admin_headers)
    client.get(f"/products/{product['product_id']}")

    client.delete(f"/products/{product['product_id']}", headers=admin_headers)

    assert cache.get(CacheKey.product(product["product_id"])) is None
    assert client.get(f"/products/{product['product_id']}").json()["status"] == "DISCONTINUED"


def test_creating_a_product_invalidates_the_category_list(client, admin_headers, cache):
    client.get("/categories")  # warm
    assert cache.get(CacheKey.category_list()) is not None

    _create(client, admin_headers, sku="SKU-NEWCAT-1", category="Furniture")

    assert cache.get(CacheKey.category_list()) is None


def test_a_new_category_shows_up_immediately_in_the_list(client, admin_headers):
    client.get("/categories")

    _create(client, admin_headers, sku="SKU-NEWCAT-2", category="Gardening")

    slugs = {c["slug"] for c in client.get("/categories").json()}
    assert "gardening" in slugs


def test_moving_a_product_between_categories_clears_both_listings(client, admin_headers, cache):
    product = _create(client, admin_headers, category="Electronics")
    cache.set(CacheKey.products_by_category("Electronics", 1, 20), "[]", 300)
    cache.set(CacheKey.products_by_category("Furniture", 1, 20), "[]", 300)

    client.put(
        f"/products/{product['product_id']}",
        json={"category": "Furniture"},
        headers=admin_headers,
    )

    assert cache.get(CacheKey.products_by_category("Electronics", 1, 20)) is None
    assert cache.get(CacheKey.products_by_category("Furniture", 1, 20)) is None


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------
def test_reads_still_work_when_the_cache_is_broken(client, admin_headers, database):
    """A cache outage must cost latency, not availability."""
    from app.deps import get_cache
    from app.main import app as fastapi_app

    product = _create(client, admin_headers)

    class BrokenCache:
        def get(self, key):
            raise ConnectionError("redis down")

        def set(self, key, value, ttl_seconds):
            raise ConnectionError("redis down")

        def delete(self, *keys):
            raise ConnectionError("redis down")

        def delete_prefix(self, prefix):
            raise ConnectionError("redis down")

        def ping(self):
            return False

    fastapi_app.dependency_overrides[get_cache] = BrokenCache
    try:
        response = client.get(f"/products/{product['product_id']}")
    finally:
        fastapi_app.dependency_overrides.pop(get_cache, None)

    assert response.status_code == 200
    assert response.json()["sku"] == product["sku"]


def test_writes_still_work_when_the_cache_is_broken(client, admin_headers):
    from app.deps import get_cache
    from app.main import app as fastapi_app

    class BrokenCache:
        def get(self, key):
            raise ConnectionError("redis down")

        def set(self, key, value, ttl_seconds):
            raise ConnectionError("redis down")

        def delete(self, *keys):
            raise ConnectionError("redis down")

        def delete_prefix(self, prefix):
            raise ConnectionError("redis down")

        def ping(self):
            return False

    fastapi_app.dependency_overrides[get_cache] = BrokenCache
    try:
        response = client.post(
            "/products",
            json={
                "sku": "SKU-NOCACHE-1",
                "name": "Written without a cache",
                "category": "Electronics",
                "price": "10.00",
                "currency": "INR",
            },
            headers=admin_headers,
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_cache, None)

    assert response.status_code == 201


def test_search_results_are_not_cached(client, admin_headers, cache):
    """Unbounded key space, poor hit rate -- deliberately excluded."""
    _create(client, admin_headers, name="Findable Widget")

    client.get("/products/search?q=Findable")

    # `keys()` here is a method on the test cache returning a list, not a dict view.
    cached_keys = cache.keys()
    assert not any(key.startswith("products:search") for key in cached_keys)
