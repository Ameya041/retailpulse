"""Payment service test fixtures."""

from __future__ import annotations

import os
import uuid

import pytest

os.environ["JWT_SECRET_KEY"] = "payment-service-test-secret-key-long-enough-1"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_db_session, get_gateway  # noqa: E402
from app.gateway import (  # noqa: E402
    AlwaysApprovesGateway,
    AlwaysDeclinesGateway,
)
from app.main import app  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "payment-service-test-secret-key-long-enough-1"

CUSTOMER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_CUSTOMER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


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
def gateway():
    """Deterministic by default -- a test suite must not depend on dice."""
    return AlwaysApprovesGateway()


@pytest.fixture()
def declining_gateway():
    return AlwaysDeclinesGateway()


@pytest.fixture()
def client(database: Database, gateway) -> TestClient:
    def _session_override():
        with database.session() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_gateway] = lambda: gateway
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
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.ADMIN, uuid.uuid4())}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, CUSTOMER_ID)}"}


@pytest.fixture()
def other_customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, OTHER_CUSTOMER_ID)}"}
