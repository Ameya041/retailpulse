"""Inventory service test fixtures.

Two tiers, deliberately:

* SQLite in-memory for logic tests -- fast, no containers, runs in CI on every
  push.
* Real Postgres for the concurrency tests, because `SELECT ... FOR UPDATE` is a
  no-op on SQLite. A test suite that only ran on SQLite would happily pass while
  the oversell bug was still present, which would make it worse than useless.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ["JWT_SECRET_KEY"] = "inventory-test-secret-key-long-enough-for-hs256"
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.service import InventoryService  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "inventory-test-secret-key-long-enough-for-hs256"


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
def client(database: Database) -> TestClient:
    def _override():
        with database.session() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _token(role: Role) -> str:
    return create_access_token(
        user_id=uuid.uuid4(),
        email=f"{role.value.lower()}@retailpulse.test",
        role=role,
        secret_key=TEST_SECRET,
        expires_minutes=30,
    )


@pytest.fixture()
def operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.WAREHOUSE_OPERATOR)}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER)}"}


@pytest.fixture()
def location(session):
    """A single stocked location."""
    return InventoryService(session).create_location("BLR01", "Bangalore Store", "Bangalore")


@pytest.fixture()
def product_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def stocked(session, location, product_id):
    """Product with 10 available units at one location."""
    from app.schemas import RestockRequest

    InventoryService(session).restock(
        RestockRequest(
            product_id=product_id,
            location_id=location.location_id,
            quantity=10,
            reorder_threshold=3,
        )
    )
    return location, product_id
