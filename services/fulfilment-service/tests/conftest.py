"""Fulfilment service test fixtures."""

from __future__ import annotations

import os
import random
import uuid

import pytest

os.environ["JWT_SECRET_KEY"] = "fulfilment-service-test-secret-key-long-enough"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_db_session, get_rng  # noqa: E402
from app.main import app  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "fulfilment-service-test-secret-key-long-enough"

CUSTOMER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_CUSTOMER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SHIPPING_ADDRESS = "42 MG Road, Bangalore, Karnataka 560001"


@pytest.fixture()
def rng() -> random.Random:
    """Fixed seed: carriers and tracking numbers must be reproducible."""
    return random.Random(20260826)  # noqa: S311 - test fixture, not cryptography


@pytest.fixture()
def database() -> Database:
    db = Database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(db.engine)
    return db


@pytest.fixture()
def session(database: Database):
    with database.session() as s:
        yield s


@pytest.fixture()
def client(database: Database, rng: random.Random) -> TestClient:
    def _session_override():
        with database.session() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_rng] = lambda: rng
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
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.WAREHOUSE_OPERATOR, uuid.uuid4())}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, CUSTOMER_ID)}"}


@pytest.fixture()
def other_customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, OTHER_CUSTOMER_ID)}"}


@pytest.fixture()
def open_fulfilment(client, staff_headers) -> dict:
    """A fulfilment in PENDING for a known customer."""
    return client.post(
        "/fulfilment",
        json={
            "order_id": str(uuid.uuid4()),
            "customer_id": str(CUSTOMER_ID),
            "shipping_address": SHIPPING_ADDRESS,
        },
        headers=staff_headers,
    ).json()


def advance(client, staff_headers, order_id: str, *steps: str) -> dict:
    """Walk a fulfilment through a sequence of REST transitions."""
    body: dict = {}
    for step in steps:
        payload = {} if step in ("ship",) else None
        response = client.post(
            f"/fulfilment/{order_id}/{step}",
            json=payload,
            headers=staff_headers,
        )
        assert response.status_code == 200, f"{step} failed: {response.json()}"
        body = response.json()
    return body
