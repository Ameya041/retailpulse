"""Analytics service test fixtures."""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

os.environ["JWT_SECRET_KEY"] = "analytics-service-test-secret-key-long-enough"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.clients import NullForecastClient, StubInventoryClient  # noqa: E402
from app.deps import get_db_session, get_forecast_client, get_inventory_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import OrderEventFact, SalesFact  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402
from retailpulse_common.db import Base, Database  # noqa: E402

TEST_SECRET = "analytics-service-test-secret-key-long-enough"

WIDGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
GADGET_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


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
def inventory_client() -> StubInventoryClient:
    return StubInventoryClient(low_stock_products=3)


@pytest.fixture()
def forecast_client() -> NullForecastClient:
    return NullForecastClient()


@pytest.fixture()
def client(database: Database, inventory_client, forecast_client) -> TestClient:
    def _override():
        with database.session() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    app.dependency_overrides[get_inventory_client] = lambda: inventory_client
    app.dependency_overrides[get_forecast_client] = lambda: forecast_client
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _token(role: Role) -> str:
    return create_access_token(
        user_id=uuid.uuid4(),
        email=f"{role.value.lower()}@retailpulse.com",
        role=role,
        secret_key=TEST_SECRET,
        expires_minutes=30,
    )


@pytest.fixture()
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.WAREHOUSE_OPERATOR)}"}


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.ADMIN)}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER)}"}


def make_fact(
    *,
    order_id=None,
    product_id=WIDGET_ID,
    sku="WIDGET-001",
    category="Electronics",
    store_id="BLR01",
    quantity=2,
    unit_price="100.00",
    days_ago=1,
) -> SalesFact:
    price = Decimal(unit_price)
    return SalesFact(
        order_id=order_id or uuid.uuid4(),
        customer_id=uuid.uuid4(),
        product_id=product_id,
        sku=sku,
        product_name=f"Product {sku}",
        category=category,
        store_id=store_id,
        quantity=quantity,
        unit_price=price,
        revenue=price * quantity,
        currency="INR",
        sale_date=date.today() - timedelta(days=days_ago),
    )


@pytest.fixture()
def seeded(database) -> None:
    """A small, hand-checkable sales history."""
    with database.session() as session:
        session.add_all(
            [
                make_fact(days_ago=1, quantity=2, unit_price="100.00"),
                make_fact(days_ago=1, quantity=3, unit_price="100.00", store_id="MAA01"),
                make_fact(
                    days_ago=2,
                    product_id=GADGET_ID,
                    sku="GADGET-001",
                    category="Home Appliances",
                    quantity=1,
                    unit_price="500.00",
                ),
                make_fact(days_ago=5, quantity=4, unit_price="100.00"),
            ]
        )
        session.add_all(
            [
                OrderEventFact(
                    order_id=uuid.uuid4(),
                    status="DELIVERED",
                    total_amount=Decimal("200.00"),
                    currency="INR",
                    occurred_on=date.today() - timedelta(days=1),
                )
                for _ in range(8)
            ]
        )
        session.add_all(
            [
                OrderEventFact(
                    order_id=uuid.uuid4(),
                    status="CANCELLED",
                    total_amount=Decimal("150.00"),
                    currency="INR",
                    occurred_on=date.today() - timedelta(days=1),
                    reason="OUT_OF_STOCK",
                )
                for _ in range(2)
            ]
        )
