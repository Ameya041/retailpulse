"""Order service test fixtures."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

os.environ["JWT_SECRET_KEY"] = "order-service-test-secret-key-long-enough-256"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.deps import get_catalog, get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.product_client import CatalogProduct, InMemoryProductCatalog  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "order-service-test-secret-key-long-enough-256"

CUSTOMER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_CUSTOMER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")

WIDGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
GADGET_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
DISCONTINUED_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
USD_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


@pytest.fixture()
def catalog() -> InMemoryProductCatalog:
    """A fake catalog, so order tests never depend on product-service running."""
    return InMemoryProductCatalog(
        {
            WIDGET_ID: CatalogProduct(
                product_id=WIDGET_ID,
                sku="WIDGET-001",
                name="Standard Widget",
                price=Decimal("199.99"),
                currency="INR",
                status="ACTIVE",
            ),
            GADGET_ID: CatalogProduct(
                product_id=GADGET_ID,
                sku="GADGET-001",
                name="Deluxe Gadget",
                price=Decimal("1500.50"),
                currency="INR",
                status="ACTIVE",
            ),
            DISCONTINUED_ID: CatalogProduct(
                product_id=DISCONTINUED_ID,
                sku="OLD-001",
                name="Discontinued Thing",
                price=Decimal("50.00"),
                currency="INR",
                status="DISCONTINUED",
            ),
            USD_ID: CatalogProduct(
                product_id=USD_ID,
                sku="IMPORT-001",
                name="Imported Item",
                price=Decimal("25.00"),
                currency="USD",
                status="ACTIVE",
            ),
        }
    )


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
def client(database: Database, catalog: InMemoryProductCatalog) -> TestClient:
    def _session_override():
        with database.session() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_catalog] = lambda: catalog
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
def other_customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER, OTHER_CUSTOMER_ID)}"}


@pytest.fixture()
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.WAREHOUSE_OPERATOR, uuid.uuid4())}"}


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.ADMIN, uuid.uuid4())}"}


SHIPPING_ADDRESS = "42 MG Road, Bangalore, Karnataka 560001"


@pytest.fixture()
def placed_order(client, customer_headers) -> dict:
    """An order in CREATED with two lines."""
    return client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [
                {"product_id": str(WIDGET_ID), "quantity": 2},
                {"product_id": str(GADGET_ID), "quantity": 1},
            ],
        },
        headers=customer_headers,
    ).json()


def advance(client, staff_headers, order_id: str, *statuses: str) -> None:
    """Walk an order through a sequence of statuses as staff."""
    for status_value in statuses:
        response = client.patch(
            f"/orders/{order_id}/status",
            json={"status": status_value},
            headers=staff_headers,
        )
        assert response.status_code == 200, (
            f"could not move to {status_value}: {response.json()}"
        )
