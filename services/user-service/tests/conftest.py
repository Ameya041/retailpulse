"""User service test fixtures."""

from __future__ import annotations

import os

import pytest

os.environ["JWT_SECRET_KEY"] = "user-service-test-secret-key-long-enough-hs256"
os.environ["ENVIRONMENT"] = "test"  # keeps the bootstrap admin seeder disabled
# Cheap bcrypt for the suite only. Guarded in retailpulse_common.auth so it
# cannot take effect outside a test environment.
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from retailpulse_common.auth import Role  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "user-service-test-secret-key-long-enough-hs256"


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


@pytest.fixture()
def customer(client) -> dict:
    """A registered, logged-in customer: record plus auth headers."""
    client.post(
        "/auth/register",
        json={
            "email": "customer@retailpulse.com",
            "password": "customer-pass-123",
            "full_name": "Casey Customer",
        },
    )
    body = client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "customer-pass-123"},
    ).json()
    return {
        "user": body["user"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


@pytest.fixture()
def admin(client, database) -> dict:
    """An admin account, promoted directly in the database.

    Deliberately not created through the API: there is no endpoint that lets
    anyone create an admin without already being one, which is the point.
    """
    client.post(
        "/auth/register",
        json={
            "email": "admin@retailpulse.com",
            "password": "admin-pass-123",
            "full_name": "Avery Admin",
        },
    )
    with database.session() as s:
        from sqlalchemy import select

        from app.models import User

        user = s.scalar(select(User).where(User.email == "admin@retailpulse.com"))
        user.role = Role.ADMIN.value

    body = client.post(
        "/auth/login",
        json={"email": "admin@retailpulse.com", "password": "admin-pass-123"},
    ).json()
    return {
        "user": body["user"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }
