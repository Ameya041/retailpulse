"""Concurrency tests against a real Postgres instance.

These are the tests that actually prove the reservation logic is correct.
Everything else in this suite runs on SQLite, where `SELECT ... FOR UPDATE` is
silently ignored -- so a SQLite-only suite would pass with the oversell bug
fully intact.

Each test launches real threads on real connections and asserts the invariant
that matters: **the number of units sold never exceeds the number of units that
existed.**

Skipped automatically when Postgres is not reachable, so `pytest` still works
on a laptop with nothing running.

Run explicitly with:  pytest -m postgres
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from sqlalchemy import text

from app.models import InventoryItem, Reservation, ReservationStatus
from app.schemas import ReleaseRequest, ReservationLine, ReserveRequest, RestockRequest
from app.service import InventoryService
from retailpulse_common.db import Base, Database
from retailpulse_common.errors import InsufficientInventoryError

POSTGRES_URL = (
    f"postgresql+psycopg://{os.getenv('POSTGRES_USER', 'retailpulse')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'retailpulse_dev_password')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('INVENTORY_DB', 'retailpulse_inventory')}"
)


def _postgres_available() -> bool:
    try:
        db = Database(POSTGRES_URL, pool_size=1, max_overflow=0)
        return db.ping()
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not _postgres_available(), reason="Postgres is not reachable; run `docker compose up -d postgres`"
    ),
]


@pytest.fixture(scope="module")
def pg() -> Database:
    # Pool must be large enough for every worker thread to hold its own
    # connection simultaneously -- otherwise threads queue on the pool and the
    # test would not actually exercise concurrency.
    db = Database(POSTGRES_URL, pool_size=25, max_overflow=15)
    Base.metadata.create_all(db.engine)
    return db


@pytest.fixture()
def pg_location(pg: Database):
    """A location unique to this test, so parallel tests never interfere."""
    code = f"T{uuid.uuid4().hex[:6].upper()}"
    with pg.session() as session:
        location = InventoryService(session).create_location(code, f"Test {code}", "Testville")
        session.flush()
        return location.location_id


def _stock(pg: Database, location_id: uuid.UUID, quantity: int) -> uuid.UUID:
    product_id = uuid.uuid4()
    with pg.session() as session:
        InventoryService(session).restock(
            RestockRequest(
                product_id=product_id, location_id=location_id, quantity=quantity
            )
        )
    return product_id


def _read(pg: Database, product_id: uuid.UUID, location_id: uuid.UUID) -> InventoryItem:
    with pg.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        session.expunge_all()
        return item


def _try_reserve(pg: Database, product_id: uuid.UUID, location_id: uuid.UUID, qty: int) -> bool:
    """One independent transaction. True if it got the stock."""
    try:
        with pg.session() as session:
            InventoryService(session).reserve(
                ReserveRequest(
                    order_id=uuid.uuid4(),
                    lines=[
                        ReservationLine(
                            product_id=product_id, quantity=qty, location_id=location_id
                        )
                    ],
                )
            )
        return True
    except InsufficientInventoryError:
        return False


# ---------------------------------------------------------------------------
# The oversell test
# ---------------------------------------------------------------------------
def test_concurrent_buyers_cannot_oversell_the_last_unit(pg, pg_location):
    """20 threads race for 1 unit. Exactly one may win."""
    product_id = _stock(pg, pg_location, 1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_try_reserve, pg, product_id, pg_location, 1) for _ in range(20)]
        outcomes = [f.result() for f in as_completed(futures)]

    item = _read(pg, product_id, pg_location)

    assert sum(outcomes) == 1, "more than one buyer got the last unit"
    assert item.available_quantity == 0
    assert item.reserved_quantity == 1
    assert item.total_quantity == 1


def test_concurrent_reservations_conserve_total_stock(pg, pg_location):
    """50 threads each want 2 units from a pool of 40. Exactly 20 may win."""
    product_id = _stock(pg, pg_location, 40)

    with ThreadPoolExecutor(max_workers=25) as pool:
        futures = [pool.submit(_try_reserve, pg, product_id, pg_location, 2) for _ in range(50)]
        outcomes = [f.result() for f in as_completed(futures)]

    item = _read(pg, product_id, pg_location)

    assert sum(outcomes) == 20
    assert item.available_quantity == 0
    assert item.reserved_quantity == 40
    # The invariant: units are conserved, never conjured.
    assert item.total_quantity == 40


def test_no_negative_stock_under_mixed_quantities(pg, pg_location):
    """Threads asking for varying amounts must still never overdraw."""
    product_id = _stock(pg, pg_location, 30)
    quantities = [1, 2, 3, 5, 7] * 8  # 144 units requested against 30 available

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [
            pool.submit(_try_reserve, pg, product_id, pg_location, q) for q in quantities
        ]
        granted = [q for q, f in zip(quantities, futures, strict=True) if f.result()]

    item = _read(pg, product_id, pg_location)

    assert item.available_quantity >= 0
    assert item.reserved_quantity == sum(granted)
    assert item.available_quantity + item.reserved_quantity == 30


def test_concurrent_reserve_and_release_conserve_stock(pg, pg_location):
    """Interleaved holds and releases must not lose or duplicate units."""
    product_id = _stock(pg, pg_location, 20)
    order_ids = [uuid.uuid4() for _ in range(10)]

    def reserve_then_release(order_id: uuid.UUID) -> None:
        with pg.session() as session:
            InventoryService(session).reserve(
                ReserveRequest(
                    order_id=order_id,
                    lines=[
                        ReservationLine(
                            product_id=product_id, quantity=2, location_id=pg_location
                        )
                    ],
                )
            )
        with pg.session() as session:
            InventoryService(session).release(ReleaseRequest(order_id=order_id))

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(reserve_then_release, order_ids))

    item = _read(pg, product_id, pg_location)

    # Everything reserved was released, so we are back where we started.
    assert item.available_quantity == 20
    assert item.reserved_quantity == 0


def test_duplicate_order_reservations_race_safely(pg, pg_location):
    """The same order_id reserved by 10 threads at once holds stock only once.

    This is the Kafka redelivery scenario: several consumers can pick up copies
    of ORDER_CREATED simultaneously. The unique index on
    (order_id, product_id, location_id) is what makes it safe.
    """
    product_id = _stock(pg, pg_location, 50)
    order_id = uuid.uuid4()

    def reserve_same_order() -> str:
        try:
            with pg.session() as session:
                _, replay = InventoryService(session).reserve(
                    ReserveRequest(
                        order_id=order_id,
                        lines=[
                            ReservationLine(
                                product_id=product_id, quantity=5, location_id=pg_location
                            )
                        ],
                    )
                )
            return "replay" if replay else "held"
        except Exception:
            # A loser of the unique-index race; correct behaviour.
            return "rejected"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = [f.result() for f in [pool.submit(reserve_same_order) for _ in range(10)]]

    item = _read(pg, product_id, pg_location)

    assert results.count("held") == 1, f"stock was held more than once: {results}"
    assert item.reserved_quantity == 5, "duplicate delivery reserved stock twice"
    assert item.available_quantity == 45


def test_multi_line_orders_do_not_deadlock(pg, pg_location):
    """Two products, two orders, opposite logical order -- must not deadlock.

    Locks are always acquired ordered by inventory_id, so no lock cycle can
    form. Without that ordering this test would intermittently raise a
    Postgres deadlock error.
    """
    product_a = _stock(pg, pg_location, 100)
    product_b = _stock(pg, pg_location, 100)

    def reserve_both(reverse: bool) -> bool:
        lines = [
            ReservationLine(product_id=product_a, quantity=1, location_id=pg_location),
            ReservationLine(product_id=product_b, quantity=1, location_id=pg_location),
        ]
        if reverse:
            lines.reverse()
        with pg.session() as session:
            InventoryService(session).reserve(
                ReserveRequest(order_id=uuid.uuid4(), lines=lines)
            )
        return True

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(reserve_both, i % 2 == 0) for i in range(40)]
        # .result() re-raises; a deadlock would surface here as a DBAPI error.
        assert all(f.result() for f in as_completed(futures))

    assert _read(pg, product_a, pg_location).reserved_quantity == 40
    assert _read(pg, product_b, pg_location).reserved_quantity == 40


def test_check_constraint_is_enforced_by_postgres(pg, pg_location):
    """The database itself refuses negative stock, independent of service code."""
    from sqlalchemy.exc import IntegrityError

    product_id = _stock(pg, pg_location, 5)

    with pytest.raises(IntegrityError), pg.session() as session:
        session.execute(
            text(
                "UPDATE inventory SET available_quantity = -1 "
                "WHERE product_id = :pid AND location_id = :lid"
            ),
            {"pid": product_id, "lid": pg_location},
        )


def test_reservation_rows_match_reserved_quantity(pg, pg_location):
    """Reconciliation: the sum of HELD reservations equals reserved_quantity."""
    product_id = _stock(pg, pg_location, 30)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: _try_reserve(pg, product_id, pg_location, 2), range(12)))

    with pg.session() as session:
        held = (
            session.query(Reservation)
            .filter_by(product_id=product_id, status=ReservationStatus.HELD.value)
            .all()
        )
        item = InventoryService(session).get_item(product_id, pg_location)
        assert sum(r.quantity for r in held) == item.reserved_quantity
