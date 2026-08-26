"""Load test for RetailPulse.

    locust -f load-tests/locustfile.py --host http://localhost:8000

    # Headless, matching the spec's 100 concurrent users:
    locust -f load-tests/locustfile.py --host http://localhost:8000 \
           --headless --users 100 --spawn-rate 10 --run-time 5m \
           --csv load-tests/results/run

## What is being measured, and what is not

This measures the **read-heavy browsing path plus checkout**, which is what a
retail platform actually serves. It is not a benchmark of any single service:
requests go through the gateway, so every number includes proxying, rate-limit
evaluation and JWT verification. That is deliberate -- it is the latency a
customer experiences, not a figure that only exists in isolation.

## Why the mix is weighted this way

Real retail traffic is overwhelmingly browsing. Modelling it as an even split
across endpoints would produce a checkout rate no real store sees, and would
make the results useless for capacity planning. The weights below are roughly
an order of magnitude apart, browse to buy.

## Reading the results honestly

* **P95 and P99 matter, the mean does not.** A mean hides the tail, and the
  tail is what users experience as "the site is slow".
* **Failures include 429s by default.** The gateway rate-limits to 100
  requests/minute/user, so a load test that hammers one account will be
  throttled *by design*. Each simulated user therefore registers its own
  account -- otherwise the test would mostly measure the rate limiter.
* Numbers from a laptop running the whole stack in Docker are not production
  numbers. Every service, both databases and Kafka are competing for the same
  cores. Quote them as "the stack on one machine", never as a capacity figure.
"""

from __future__ import annotations

import random
import uuid

from locust import HttpUser, between, events, task

SHIPPING_ADDRESSES = [
    "42 MG Road, Bangalore, Karnataka 560001",
    "17 Anna Salai, Chennai, Tamil Nadu 600002",
    "8 Banjara Hills, Hyderabad, Telangana 500034",
    "23 Marine Drive, Mumbai, Maharashtra 400020",
    "5 Connaught Place, New Delhi, Delhi 110001",
]

SEARCH_TERMS = ["samsung", "widget", "tv", "cotton", "steel", "pro", "air", "smart"]


class ShopperUser(HttpUser):
    """Simulates one customer browsing and occasionally buying."""

    # Think time. Without it Locust generates a request storm no human
    # produces, and the resulting latency figures describe a benchmark rather
    # than a shop.
    wait_time = between(1, 4)

    def on_start(self) -> None:
        """Register and sign in.

        Each simulated user gets its own account. Sharing one would put every
        virtual user in the same rate-limit bucket, and the test would end up
        measuring the rate limiter rather than the application.
        """
        self.token = None
        self.product_ids: list[str] = []

        email = f"loadtest-{uuid.uuid4().hex[:12]}@retailpulse.com"
        # Not a credential: a throwaway password for an account this run
        # creates and never uses again.
        password = "load-test-password-123"  # noqa: S105

        with self.client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "full_name": "Load Test Shopper"},
            name="POST /auth/register",
            catch_response=True,
        ) as response:
            if response.status_code not in (201, 409):
                response.failure(f"registration failed: {response.status_code}")
                return
            response.success()

        with self.client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
            name="POST /auth/login",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"login failed: {response.status_code}")
                return
            self.token = response.json()["access_token"]
            response.success()

        self._load_catalog()

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _load_catalog(self) -> None:
        """Cache some product IDs so later tasks act on real data."""
        with self.client.get(
            "/api/products?page=1&page_size=20",
            name="GET /products",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                self.product_ids = [item["product_id"] for item in response.json()["items"]]
                response.success()
            else:
                response.failure(f"catalog load failed: {response.status_code}")

    # ------------------------------------------------------------------
    # Browsing -- the overwhelming majority of real traffic.
    # ------------------------------------------------------------------
    @task(30)
    def browse_catalog(self) -> None:
        page = random.randint(1, 3)
        self.client.get(
            f"/api/products?page={page}&page_size=20",
            # Explicit name: without it, every page number becomes its own row
            # in the statistics table and the summary is unreadable.
            name="GET /products",
        )

    @task(20)
    def view_product(self) -> None:
        if not self.product_ids:
            return
        product_id = random.choice(self.product_ids)
        # The hottest read in the system, and the one served from Redis.
        self.client.get(f"/api/products/{product_id}", name="GET /products/{id}")

    @task(12)
    def search(self) -> None:
        term = random.choice(SEARCH_TERMS)
        self.client.get(f"/api/products/search?q={term}", name="GET /products/search")

    @task(8)
    def check_stock(self) -> None:
        if not self.product_ids:
            return
        product_id = random.choice(self.product_ids)
        self.client.get(f"/api/inventory/{product_id}", name="GET /inventory/{id}")

    @task(6)
    def view_categories(self) -> None:
        self.client.get("/api/categories", name="GET /categories")

    # ------------------------------------------------------------------
    # Buying -- rarer, and far more expensive per request.
    # ------------------------------------------------------------------
    @task(5)
    def add_to_cart(self) -> None:
        if not self.product_ids or not self.token:
            return
        self.client.post(
            "/api/cart/items",
            json={"product_id": random.choice(self.product_ids), "quantity": random.randint(1, 3)},
            headers=self.auth,
            name="POST /cart/items",
        )

    @task(4)
    def view_orders(self) -> None:
        if not self.token:
            return
        self.client.get("/api/orders?page=1&page_size=10", headers=self.auth, name="GET /orders")

    @task(2)
    def checkout(self) -> None:
        """The full write path: prices the cart, writes the order, stages the event.

        The most expensive request in the mix by a wide margin, and the one
        whose P95 actually matters commercially.
        """
        if not self.product_ids or not self.token:
            return

        lines = [
            {"product_id": product_id, "quantity": random.randint(1, 2)}
            for product_id in random.sample(self.product_ids, min(2, len(self.product_ids)))
        ]

        with self.client.post(
            "/api/orders",
            json={"shipping_address": random.choice(SHIPPING_ADDRESSES), "lines": lines},
            headers=self.auth,
            name="POST /orders",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 409:
                # Out of stock is a correct business outcome under sustained
                # load, not a defect. Counting it as a failure would make the
                # error rate meaningless.
                response.success()
            elif response.status_code == 429:
                response.failure("rate limited -- the load exceeds the configured limit")
            else:
                response.failure(f"checkout failed: {response.status_code}")


class StaffUser(HttpUser):
    """A much smaller population running the expensive analytical queries."""

    wait_time = between(5, 15)
    # One staff user per twenty shoppers, which is roughly the real ratio and
    # keeps dashboards from dominating the load profile.
    weight = 1

    def on_start(self) -> None:
        self.token = None
        # Staff accounts cannot be self-registered -- the API refuses to grant
        # a privileged role. Supply a real token to exercise these paths:
        #   locust ... --tags staff  (with RETAILPULSE_STAFF_TOKEN set)
        import os

        self.token = os.getenv("RETAILPULSE_STAFF_TOKEN")

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @task(5)
    def dashboard(self) -> None:
        if not self.token:
            return
        self.client.get("/api/analytics/dashboard?days=30", headers=self.auth, name="GET /analytics/dashboard")

    @task(3)
    def sales_breakdown(self) -> None:
        if not self.token:
            return
        self.client.get("/api/analytics/sales/by-category?days=30", headers=self.auth, name="GET /analytics/by-category")

    @task(1)
    def forecast(self) -> None:
        if not self.token:
            return
        self.client.get("/api/forecast/products", headers=self.auth, name="GET /forecast/products")


@events.quitting.add_listener
def _assert_acceptable(environment, **_kwargs) -> None:
    """Fail the run if the results are bad enough that they should block a release.

    A load test that always exits 0 is a report nobody reads. These thresholds
    turn it into a gate: CI can run it and the build fails on a regression.
    """
    stats = environment.stats.total

    if stats.num_requests == 0:
        print("\nNo requests were made -- is the stack running?")
        environment.process_exit_code = 1
        return

    failure_ratio = stats.fail_ratio
    p95 = stats.get_response_time_percentile(0.95)
    p99 = stats.get_response_time_percentile(0.99)

    print("\n" + "=" * 60)
    print(f"  requests      {stats.num_requests:,}")
    print(f"  failures      {stats.num_failures:,} ({failure_ratio * 100:.2f}%)")
    print(f"  requests/sec  {stats.total_rps:.1f}")
    print(f"  median        {stats.median_response_time} ms")
    print(f"  P95           {p95} ms")
    print(f"  P99           {p99} ms")
    print("=" * 60)

    if failure_ratio > 0.01:
        print(f"FAIL: failure rate {failure_ratio * 100:.2f}% exceeds 1%")
        environment.process_exit_code = 1
    elif p95 > 1000:
        print(f"FAIL: P95 {p95} ms exceeds 1000 ms")
        environment.process_exit_code = 1
    else:
        environment.process_exit_code = 0
