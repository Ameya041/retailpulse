"""API gateway test fixtures.

Upstream services are faked with an httpx MockTransport, so the gateway's own
behaviour -- routing, rate limiting, breakers, header handling -- is tested
without running six other services.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

os.environ["JWT_SECRET_KEY"] = "gateway-test-secret-key-long-enough-for-hs256"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app.deps import get_breakers, get_http_client, get_rate_limiter  # noqa: E402
from app.main import app  # noqa: E402
from app.routing import BreakerRegistry  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.rate_limit import InMemoryRateLimiter  # noqa: E402

TEST_SECRET = "gateway-test-secret-key-long-enough-for-hs256"
CUSTOMER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class Upstream:
    """Records what the gateway forwarded, and decides what to reply."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.json_body: dict = {"ok": True}
        self.raise_exc: Exception | None = None
        self.response_headers: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(
            self.status_code, json=self.json_body, headers=self.response_headers
        )

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the gateway forwarded nothing"
        return self.requests[-1]


@pytest.fixture()
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture()
def limiter() -> InMemoryRateLimiter:
    return InMemoryRateLimiter()


@pytest.fixture()
def breakers() -> BreakerRegistry:
    return BreakerRegistry(failure_threshold=3, cool_down_seconds=15.0)


@pytest.fixture()
def client(upstream: Upstream, limiter, breakers) -> TestClient:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))

    app.dependency_overrides[get_http_client] = lambda: mock_client
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_breakers] = lambda: breakers

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _token(role: Role, user_id: uuid.UUID) -> str:
    return create_access_token(
        user_id=user_id,
        email=f"{role.value.lower()}@retailpulse.com",
        role=role,
        secret_key=TEST_SECRET,
        expires_minutes=30,
    )


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, CUSTOMER_ID)}"}


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.ADMIN, uuid.uuid4())}"}
