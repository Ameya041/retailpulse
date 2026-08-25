"""Test fixtures for the product service.

Unit/API tests run against an in-memory SQLite database so the suite is fast
and needs no containers -- which matters for CI. Anything that depends on
Postgres-specific behaviour (row locking, NUMERIC precision) is covered by the
integration tests that run against real Postgres in Compose.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ["JWT_SECRET_KEY"] = "test-secret-key-not-used-in-production"
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "test-secret-key-not-used-in-production"


@pytest.fixture()
def database() -> Database:
    db = Database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(db.engine)
    return db


@pytest.fixture()
def client(database: Database) -> TestClient:
    def _override():
        with database.session() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def session(database: Database):
    with database.session() as s:
        yield s


def _token(role: Role) -> str:
    return create_access_token(
        user_id=uuid.uuid4(),
        email=f"{role.value.lower()}@retailpulse.test",
        role=role,
        secret_key=TEST_SECRET,
        expires_minutes=30,
    )


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.ADMIN)}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER)}"}


@pytest.fixture()
def sample_product() -> dict:
    return {
        "sku": "TV-SAM-55U8",
        "name": 'Samsung 55" Crystal UHD TV',
        "description": "4K Crystal UHD smart television with HDR10+.",
        "category": "Electronics",
        "brand": "Samsung",
        "price": "48999.00",
        "currency": "INR",
        "weight_grams": 15200,
    }
