"""Reservation logic tests.

The invariant under test throughout:

    available + reserved is conserved by reserve/release,
    and neither column ever goes negative.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.models import InventoryItem, MovementType, Reservation, ReservationStatus, StockMovement
from app.schemas import ReleaseRequest, ReservationLine, ReserveRequest, RestockRequest
from app.service import InventoryService
from retailpulse_common.errors import (
    ConflictError,
    InsufficientInventoryError,
    NotFoundError,
    ValidationError,
)


def _reserve(service, order_id, product_id, quantity, location_id=None):
    return service.reserve(
        ReserveRequest(
            order_id=order_id,
            lines=[
                ReservationLine(
                    product_id=product_id, quantity=quantity, location_id=location_id
                )
            ],
        )
    )


# ---------------------------------------------------------------------------
# Restock
# ---------------------------------------------------------------------------
def test_restock_creates_the_row_on_first_delivery(session, location, product_id):
    item = InventoryService(session).restock(
        RestockRequest(
            product_id=product_id, location_id=location.location_id, quantity=25,
            reorder_threshold=5,
        )
    )
    assert item.available_quantity == 25
    assert item.reserved_quantity == 0
    assert item.reorder_threshold == 5


def test_restock_accumulates_on_subsequent_deliveries(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    item = service.restock(
        RestockRequest(product_id=product_id, location_id=location.location_id, quantity=15)
    )
    assert item.available_quantity == 25


def test_restock_at_unknown_location_is_rejected(session, product_id):
    with pytest.raises(NotFoundError):
        InventoryService(session).restock(
            RestockRequest(
                product_id=product_id, location_id=uuid.uuid4(), quantity=5
            )
        )


def test_restock_records_a_movement(session, stocked):
    location, product_id = stocked
    movements = InventoryService(session).movements(product_id)
    assert len(movements) == 1
    assert movements[0].movement_type == MovementType.RESTOCK.value
    assert movements[0].quantity_delta == 10
    assert movements[0].available_after == 10


# ---------------------------------------------------------------------------
# Reserve -- the core invariant
# ---------------------------------------------------------------------------
def test_reserve_moves_units_from_available_to_reserved(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    allocations, replay = _reserve(service, uuid.uuid4(), product_id, 3, location.location_id)

    item = service.get_item(product_id, location.location_id)
    assert replay is False
    assert item.available_quantity == 7
    assert item.reserved_quantity == 3
    # Conservation: nothing was created or destroyed.
    assert item.total_quantity == 10
    assert sum(a.quantity for a in allocations) == 3


def test_reserve_exactly_all_stock_is_allowed(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    _reserve(service, uuid.uuid4(), product_id, 10, location.location_id)

    item = service.get_item(product_id, location.location_id)
    assert item.available_quantity == 0
    assert item.reserved_quantity == 10


def test_reserving_more_than_available_is_rejected(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    with pytest.raises(InsufficientInventoryError) as exc:
        _reserve(service, uuid.uuid4(), product_id, 11, location.location_id)

    assert exc.value.status_code == 409
    assert exc.value.details["requested"] == 11
    assert exc.value.details["available"] == 10


def test_failed_reservation_leaves_stock_untouched(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    with pytest.raises(InsufficientInventoryError):
        _reserve(service, uuid.uuid4(), product_id, 99, location.location_id)

    item = service.get_item(product_id, location.location_id)
    assert item.available_quantity == 10
    assert item.reserved_quantity == 0


def test_stock_can_never_go_negative_across_a_sequence(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    # The fourth reservation must fail -- 10 units cannot satisfy 4 x 3.
    for _ in range(4):
        with contextlib.suppress(InsufficientInventoryError):
            _reserve(service, uuid.uuid4(), product_id, 3, location.location_id)

    item = service.get_item(product_id, location.location_id)
    assert item.available_quantity >= 0
    assert item.reserved_quantity >= 0
    assert item.total_quantity == 10


def test_reserving_a_product_with_no_inventory_records_is_404(session):
    with pytest.raises(NotFoundError):
        _reserve(InventoryService(session), uuid.uuid4(), uuid.uuid4(), 1)


# ---------------------------------------------------------------------------
# Multi-line orders are all-or-nothing
# ---------------------------------------------------------------------------
def test_multi_line_reservation_is_atomic(session, location, product_id):
    """If any line cannot be satisfied, no line holds stock."""
    service = InventoryService(session)
    second_product = uuid.uuid4()
    service.restock(
        RestockRequest(product_id=product_id, location_id=location.location_id, quantity=10)
    )
    service.restock(
        RestockRequest(product_id=second_product, location_id=location.location_id, quantity=2)
    )

    with pytest.raises(InsufficientInventoryError):
        service.reserve(
            ReserveRequest(
                order_id=uuid.uuid4(),
                lines=[
                    ReservationLine(
                        product_id=product_id, quantity=5, location_id=location.location_id
                    ),
                    # This line cannot be met -- it must roll the first one back.
                    ReservationLine(
                        product_id=second_product, quantity=50, location_id=location.location_id
                    ),
                ],
            )
        )

    session.rollback()
    # Nothing was held for either product.
    assert (
        session.query(Reservation).filter_by(status=ReservationStatus.HELD.value).count() == 0
    )


def test_duplicate_lines_in_one_request_are_rejected(product_id):
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        ReserveRequest(
            order_id=uuid.uuid4(),
            lines=[
                ReservationLine(product_id=product_id, quantity=1),
                ReservationLine(product_id=product_id, quantity=2),
            ],
        )


# ---------------------------------------------------------------------------
# Multi-location allocation
# ---------------------------------------------------------------------------
def test_allocation_spans_locations_when_none_is_pinned(session, product_id):
    service = InventoryService(session)
    blr = service.create_location("BLR01", "Bangalore Store", "Bangalore")
    maa = service.create_location("MAA01", "Chennai Store", "Chennai")
    service.restock(
        RestockRequest(product_id=product_id, location_id=blr.location_id, quantity=4)
    )
    service.restock(
        RestockRequest(product_id=product_id, location_id=maa.location_id, quantity=6)
    )

    allocations, _ = _reserve(service, uuid.uuid4(), product_id, 8)

    assert sum(a.quantity for a in allocations) == 8
    assert len(allocations) == 2
    # Largest stock first: Chennai (6) is drained before Bangalore.
    assert allocations[0].location_code == "MAA01"
    assert allocations[0].quantity == 6
    assert allocations[1].quantity == 2


def test_network_wide_shortfall_is_rejected(session, product_id):
    service = InventoryService(session)
    blr = service.create_location("BLR01", "Bangalore Store", "Bangalore")
    maa = service.create_location("MAA01", "Chennai Store", "Chennai")
    service.restock(
        RestockRequest(product_id=product_id, location_id=blr.location_id, quantity=2)
    )
    service.restock(
        RestockRequest(product_id=product_id, location_id=maa.location_id, quantity=3)
    )

    with pytest.raises(InsufficientInventoryError) as exc:
        _reserve(service, uuid.uuid4(), product_id, 6)

    assert exc.value.details["available"] == 5


# ---------------------------------------------------------------------------
# Idempotency -- Kafka redelivers
# ---------------------------------------------------------------------------
def test_reserving_the_same_order_twice_does_not_double_hold(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()

    _reserve(service, order_id, product_id, 3, location.location_id)
    allocations, replay = _reserve(service, order_id, product_id, 3, location.location_id)

    item = service.get_item(product_id, location.location_id)
    assert replay is True
    assert item.available_quantity == 7  # not 4
    assert item.reserved_quantity == 3
    assert sum(a.quantity for a in allocations) == 3


def test_releasing_twice_does_not_double_credit(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 4, location.location_id)

    service.release(ReleaseRequest(order_id=order_id))
    lines, units, replay = service.release(ReleaseRequest(order_id=order_id))

    item = service.get_item(product_id, location.location_id)
    assert replay is True
    assert (lines, units) == (0, 0)
    assert item.available_quantity == 10  # not 14
    assert item.reserved_quantity == 0


# ---------------------------------------------------------------------------
# Release and commit
# ---------------------------------------------------------------------------
def test_release_returns_units_to_available(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 6, location.location_id)

    lines, units, replay = service.release(
        ReleaseRequest(order_id=order_id, reason="PAYMENT_FAILED")
    )

    item = service.get_item(product_id, location.location_id)
    assert (lines, units, replay) == (1, 6, False)
    assert item.available_quantity == 10
    assert item.reserved_quantity == 0


def test_release_of_an_unknown_order_is_404(session):
    with pytest.raises(NotFoundError):
        InventoryService(session).release(ReleaseRequest(order_id=uuid.uuid4()))


def test_commit_consumes_reserved_stock_permanently(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 4, location.location_id)

    lines, units, replay = service.commit(order_id)

    item = service.get_item(product_id, location.location_id)
    assert (lines, units, replay) == (1, 4, False)
    assert item.reserved_quantity == 0
    assert item.available_quantity == 6  # untouched by the commit
    assert item.total_quantity == 6      # the 4 units left the building


def test_committed_stock_cannot_then_be_released(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 4, location.location_id)
    service.commit(order_id)

    lines, units, replay = service.release(ReleaseRequest(order_id=order_id))

    item = service.get_item(product_id, location.location_id)
    assert replay is True
    assert (lines, units) == (0, 0)
    assert item.available_quantity == 6  # the units did not come back


def test_commit_twice_is_a_no_op(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 2, location.location_id)
    service.commit(order_id)

    lines, units, replay = service.commit(order_id)

    assert replay is True
    assert (lines, units) == (0, 0)


# ---------------------------------------------------------------------------
# Movements ledger
# ---------------------------------------------------------------------------
def test_every_change_is_recorded_in_the_ledger(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    order_id = uuid.uuid4()
    _reserve(service, order_id, product_id, 3, location.location_id)
    service.release(ReleaseRequest(order_id=order_id))

    types = [m.movement_type for m in service.movements(product_id)]

    assert types.count(MovementType.RESTOCK.value) == 1
    assert types.count(MovementType.RESERVE.value) == 1
    assert types.count(MovementType.RELEASE.value) == 1


def test_ledger_totals_reconcile_with_current_stock(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    _reserve(service, uuid.uuid4(), product_id, 3, location.location_id)

    movements = session.query(StockMovement).filter_by(product_id=product_id).all()
    latest = max(movements, key=lambda m: m.created_at)
    item = service.get_item(product_id, location.location_id)

    assert latest.available_after == item.available_quantity
    assert latest.reserved_after == item.reserved_quantity


# ---------------------------------------------------------------------------
# Low stock and adjustments
# ---------------------------------------------------------------------------
def test_low_stock_lists_items_at_or_below_threshold(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)
    _reserve(service, uuid.uuid4(), product_id, 8, location.location_id)  # leaves 2, threshold 3

    low = service.low_stock()

    assert len(low) == 1
    assert low[0].product_id == product_id


def test_adjustment_cannot_drive_stock_negative(session, stocked):
    location, product_id = stocked
    with pytest.raises(ValidationError):
        InventoryService(session).adjust(product_id, location.location_id, -50, "stock count")


def test_zero_adjustment_is_rejected(session, stocked):
    location, product_id = stocked
    with pytest.raises(ValidationError):
        InventoryService(session).adjust(product_id, location.location_id, 0, "noop")


def test_adjustment_applies_and_is_recorded(session, stocked):
    location, product_id = stocked
    service = InventoryService(session)

    item = service.adjust(product_id, location.location_id, -2, "damaged units")

    assert item.available_quantity == 8
    # Selected by type rather than by position: two movements written in the
    # same clock tick share a created_at, and the tie-break is deterministic
    # but not insertion-ordered. See StockMovement in models.py.
    adjustments = [
        m
        for m in service.movements(product_id)
        if m.movement_type == MovementType.ADJUSTMENT.value
    ]
    assert len(adjustments) == 1
    assert adjustments[0].quantity_delta == -2
    assert adjustments[0].available_after == 8
    assert adjustments[0].note == "damaged units"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def test_duplicate_location_code_is_rejected(session, location):
    with pytest.raises(ConflictError):
        InventoryService(session).create_location("BLR01", "Another", "Bangalore")


def test_database_check_constraint_blocks_negative_stock(session, stocked):
    """Belt and braces: even a direct write is refused by the database."""
    from sqlalchemy.exc import IntegrityError

    location, product_id = stocked
    item = session.query(InventoryItem).filter_by(product_id=product_id).one()
    item.available_quantity = -1

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
