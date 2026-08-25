"""Inventory business logic -- the correctness-critical part of RetailPulse.

## The problem

Two customers order the last unit at the same instant. The naive
implementation reads the row, checks `available >= 1`, then writes
`available - 1`. Both requests read `available = 1`, both pass the check, and
both write `0`. Two units were sold; one existed. This is a lost update, and it
is invisible in testing because it only appears under concurrency.

## The fix

Every quantity change happens inside one database transaction that takes a
**row-level exclusive lock** first:

    SELECT ... FROM inventory WHERE ... FOR UPDATE

The second transaction blocks on that lock until the first commits, then reads
the *updated* value and correctly fails its check. Postgres serialises the two
requests on exactly the rows involved -- not the whole table -- so throughput
stays high.

Why not optimistic locking (a version column, retry on mismatch)? It works, but
under contention for a hot product every retry is wasted work, and the retry
loop is another thing to get wrong. Pessimistic locking on a single short
transaction is simpler and the lock is held for microseconds.

Why not just rely on the CHECK constraint? The constraint prevents the *bad
write*, but it surfaces as an IntegrityError after the fact. The lock lets the
service return a clean 409 with the actual available quantity, which the order
service needs in order to explain the failure.

## Deadlock avoidance

An order touching several products locks several rows. If order A locks
(P1, P2) and order B locks (P2, P1) at the same time, they deadlock. Every
lock acquisition here is therefore ordered by a deterministic key
(`inventory_id`), so all transactions walk the rows in the same sequence and a
cycle cannot form.

## Atomicity across lines

A reservation is all-or-nothing. If line 3 of a 5-line order cannot be
satisfied, the whole transaction rolls back and nothing is held. A partially
reserved order would leave stock stranded and the customer's order in a state
no downstream service knows how to resolve.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models import (
    InventoryItem,
    Location,
    MovementType,
    Reservation,
    ReservationStatus,
    StockMovement,
)
from app.schemas import ReleaseRequest, ReserveRequest, RestockRequest
from retailpulse_common.errors import (
    ConflictError,
    InsufficientInventoryError,
    NotFoundError,
    ValidationError,
)
from retailpulse_common.observability import (
    INVENTORY_RESERVATION_FAILURES_TOTAL,
    INVENTORY_RESERVATIONS_TOTAL,
)

logger = logging.getLogger("inventory-service")
SERVICE = "inventory-service"


class Allocation:
    """One (location, quantity) slice of a reserved line."""

    __slots__ = ("reservation_id", "product_id", "location_id", "location_code", "quantity")

    def __init__(
        self,
        reservation_id: uuid.UUID,
        product_id: uuid.UUID,
        location_id: uuid.UUID,
        location_code: str,
        quantity: int,
    ) -> None:
        self.reservation_id = reservation_id
        self.product_id = product_id
        self.location_id = location_id
        self.location_code = location_code
        self.quantity = quantity


class InventoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    def create_location(self, code: str, name: str, city: str) -> Location:
        location = Location(code=code.upper(), name=name, city=city)
        self.session.add(location)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(
                f"Location code {code} already exists.", details={"code": code}
            ) from exc
        return location

    def list_locations(self, *, active_only: bool = True) -> Sequence[Location]:
        stmt = select(Location).order_by(Location.code)
        if active_only:
            stmt = stmt.where(Location.is_active.is_(True))
        return self.session.scalars(stmt).all()

    def get_location(self, location_id: uuid.UUID) -> Location:
        location = self.session.get(Location, location_id)
        if location is None:
            raise NotFoundError(
                f"Location {location_id} was not found.",
                details={"location_id": str(location_id)},
            )
        return location

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_product_inventory(self, product_id: uuid.UUID) -> list[InventoryItem]:
        """All locations holding this product. Empty list is a valid answer."""
        stmt = (
            select(InventoryItem)
            .where(InventoryItem.product_id == product_id)
            .options(joinedload(InventoryItem.location))
            .order_by(InventoryItem.available_quantity.desc())
        )
        return list(self.session.scalars(stmt).unique().all())

    def get_item(self, product_id: uuid.UUID, location_id: uuid.UUID) -> InventoryItem:
        item = self.session.scalar(
            select(InventoryItem)
            .where(
                InventoryItem.product_id == product_id,
                InventoryItem.location_id == location_id,
            )
            .options(joinedload(InventoryItem.location))
        )
        if item is None:
            raise NotFoundError(
                "No inventory record for that product at that location.",
                details={"product_id": str(product_id), "location_id": str(location_id)},
            )
        return item

    def low_stock(self, limit: int = 100) -> list[InventoryItem]:
        """Rows at or below their reorder threshold."""
        stmt = (
            select(InventoryItem)
            .where(InventoryItem.available_quantity <= InventoryItem.reorder_threshold)
            .options(joinedload(InventoryItem.location))
            .order_by(
                (InventoryItem.reorder_threshold - InventoryItem.available_quantity).desc()
            )
            .limit(limit)
        )
        return list(self.session.scalars(stmt).unique().all())

    def movements(
        self, product_id: uuid.UUID, limit: int = 50
    ) -> list[StockMovement]:
        stmt = (
            select(StockMovement)
            .where(StockMovement.product_id == product_id)
            .order_by(StockMovement.created_at.desc(), StockMovement.movement_id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # Locking helpers
    # ------------------------------------------------------------------
    def _lock_items_for_product(self, product_id: uuid.UUID) -> list[InventoryItem]:
        """Lock every inventory row for a product, in a deterministic order.

        ``ORDER BY inventory_id`` is what prevents deadlocks: all concurrent
        transactions acquire these locks in the same sequence.

        ``with_for_update`` is a no-op on SQLite (used only by unit tests);
        the real concurrency guarantee is exercised by the Postgres
        integration tests.
        """
        stmt = (
            select(InventoryItem)
            .where(InventoryItem.product_id == product_id)
            .order_by(InventoryItem.inventory_id)
            .with_for_update()
        )
        return list(self.session.scalars(stmt).unique().all())

    def _lock_item(self, product_id: uuid.UUID, location_id: uuid.UUID) -> InventoryItem:
        stmt = (
            select(InventoryItem)
            .where(
                InventoryItem.product_id == product_id,
                InventoryItem.location_id == location_id,
            )
            .with_for_update()
        )
        item = self.session.scalar(stmt)
        if item is None:
            raise NotFoundError(
                "No inventory record for that product at that location.",
                details={"product_id": str(product_id), "location_id": str(location_id)},
            )
        return item

    def _record_movement(
        self,
        item: InventoryItem,
        movement_type: MovementType,
        delta: int,
        reference_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> None:
        self.session.add(
            StockMovement(
                product_id=item.product_id,
                location_id=item.location_id,
                movement_type=movement_type.value,
                quantity_delta=delta,
                available_after=item.available_quantity,
                reserved_after=item.reserved_quantity,
                reference_id=reference_id,
                note=note,
            )
        )
        # Sessions run with autoflush disabled, so without this the ledger row
        # would still be pending and invisible to a read issued later in the
        # same transaction.
        self.session.flush()

    # ------------------------------------------------------------------
    # Reserve
    # ------------------------------------------------------------------
    def reserve(self, request: ReserveRequest) -> tuple[list[Allocation], bool]:
        """Hold stock for an order. All-or-nothing.

        Returns ``(allocations, idempotent_replay)``. When the order was
        already reserved, the existing allocations are returned and no new
        stock is held -- this is what makes a redelivered ORDER_CREATED event
        safe.
        """
        existing = self._existing_reservations(request.order_id)
        if existing:
            logger.info(
                "reservation replay ignored",
                extra={"order_id": str(request.order_id), "lines": len(existing)},
            )
            return existing, True

        allocations: list[Allocation] = []

        # Process lines in a stable order so that two concurrent multi-line
        # orders lock rows in the same sequence.
        for line in sorted(request.lines, key=lambda item: str(item.product_id)):
            if line.location_id is not None:
                allocations.extend(
                    self._reserve_at_location(
                        request.order_id, line.product_id, line.location_id, line.quantity
                    )
                )
            else:
                allocations.extend(
                    self._reserve_across_locations(
                        request.order_id, line.product_id, line.quantity
                    )
                )

        try:
            self.session.flush()
        except IntegrityError as exc:
            # Two concurrent reserves for the same order: the unique index on
            # (order_id, product_id, location_id) rejects the loser. Treat it
            # as a conflict rather than a 500.
            self.session.rollback()
            raise ConflictError(
                "A reservation for this order is already being processed.",
                details={"order_id": str(request.order_id)},
            ) from exc

        INVENTORY_RESERVATIONS_TOTAL.labels(SERVICE).inc(len(allocations))
        logger.info(
            "inventory reserved",
            extra={
                "order_id": str(request.order_id),
                "allocations": len(allocations),
                "units": sum(a.quantity for a in allocations),
            },
        )
        return allocations, False

    def _existing_reservations(self, order_id: uuid.UUID) -> list[Allocation]:
        rows = self.session.scalars(
            select(Reservation).where(
                Reservation.order_id == order_id,
                Reservation.status == ReservationStatus.HELD.value,
            )
        ).all()
        if not rows:
            return []
        codes = self._location_codes({r.location_id for r in rows})
        return [
            Allocation(
                r.reservation_id, r.product_id, r.location_id, codes[r.location_id], r.quantity
            )
            for r in rows
        ]

    def _location_codes(self, location_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not location_ids:
            return {}
        rows = self.session.execute(
            select(Location.location_id, Location.code).where(
                Location.location_id.in_(location_ids)
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    def _reserve_at_location(
        self,
        order_id: uuid.UUID,
        product_id: uuid.UUID,
        location_id: uuid.UUID,
        quantity: int,
    ) -> list[Allocation]:
        item = self._lock_item(product_id, location_id)

        if item.available_quantity < quantity:
            INVENTORY_RESERVATION_FAILURES_TOTAL.labels(SERVICE, "insufficient_stock").inc()
            raise InsufficientInventoryError(
                "Insufficient stock at the requested location.",
                details={
                    "product_id": str(product_id),
                    "location_id": str(location_id),
                    "requested": quantity,
                    "available": item.available_quantity,
                },
            )

        # The invariant: units move between columns, they are never created.
        item.available_quantity -= quantity
        item.reserved_quantity += quantity

        reservation = Reservation(
            order_id=order_id,
            product_id=product_id,
            location_id=location_id,
            quantity=quantity,
            status=ReservationStatus.HELD.value,
        )
        self.session.add(reservation)
        self.session.flush()
        self._record_movement(item, MovementType.RESERVE, -quantity, order_id)

        return [
            Allocation(
                reservation.reservation_id, product_id, location_id, item.location.code, quantity
            )
        ]

    def _reserve_across_locations(
        self, order_id: uuid.UUID, product_id: uuid.UUID, quantity: int
    ) -> list[Allocation]:
        """Greedy allocation when the caller does not pin a location.

        Largest-stock-first keeps small locations intact for customers who are
        near them, and minimises the number of shipments for a single line.
        """
        items = self._lock_items_for_product(product_id)
        if not items:
            INVENTORY_RESERVATION_FAILURES_TOTAL.labels(SERVICE, "no_inventory_record").inc()
            raise NotFoundError(
                "This product has no inventory records at any location.",
                details={"product_id": str(product_id)},
            )

        network_available = sum(i.available_quantity for i in items)
        if network_available < quantity:
            INVENTORY_RESERVATION_FAILURES_TOTAL.labels(SERVICE, "insufficient_stock").inc()
            raise InsufficientInventoryError(
                "Insufficient stock across all locations.",
                details={
                    "product_id": str(product_id),
                    "requested": quantity,
                    "available": network_available,
                },
            )

        allocations: list[Allocation] = []
        remaining = quantity
        # Sort a copy by stock for the allocation decision; the *locks* were
        # already taken in inventory_id order above, which is what matters.
        for item in sorted(items, key=lambda i: i.available_quantity, reverse=True):
            if remaining == 0:
                break
            take = min(item.available_quantity, remaining)
            if take == 0:
                continue

            item.available_quantity -= take
            item.reserved_quantity += take
            remaining -= take

            reservation = Reservation(
                order_id=order_id,
                product_id=product_id,
                location_id=item.location_id,
                quantity=take,
                status=ReservationStatus.HELD.value,
            )
            self.session.add(reservation)
            self.session.flush()
            self._record_movement(item, MovementType.RESERVE, -take, order_id)
            allocations.append(
                Allocation(
                    reservation.reservation_id,
                    product_id,
                    item.location_id,
                    item.location.code,
                    take,
                )
            )

        if remaining != 0:
            # Unreachable given the network_available check above, but a bare
            # `assert` would be stripped under `python -O` -- and silently
            # under-allocating stock is exactly the failure this service exists
            # to prevent. Raising forces the transaction to roll back.
            raise InsufficientInventoryError(
                "Allocation could not be completed; stock changed during allocation.",
                details={
                    "product_id": str(product_id),
                    "requested": quantity,
                    "unallocated": remaining,
                },
            )
        return allocations

    # ------------------------------------------------------------------
    # Release and commit
    # ------------------------------------------------------------------
    def release(self, request: ReleaseRequest) -> tuple[int, int, bool]:
        """Return held stock to available.

        Compensating action for a failed payment or a cancellation. Returns
        ``(lines, units, idempotent_replay)``.
        """
        reservations = self.session.scalars(
            select(Reservation)
            .where(
                Reservation.order_id == request.order_id,
                Reservation.status == ReservationStatus.HELD.value,
            )
            .order_by(Reservation.reservation_id)
            .with_for_update()
        ).all()

        if not reservations:
            # Either the order was never reserved, or this release already ran.
            # Both are safe no-ops -- releasing twice must not credit stock twice.
            already = self.session.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.order_id == request.order_id)
            )
            if already:
                logger.info("release replay ignored", extra={"order_id": str(request.order_id)})
                return 0, 0, True
            raise NotFoundError(
                "No reservation exists for that order.",
                details={"order_id": str(request.order_id)},
            )

        units = 0
        for reservation in reservations:
            item = self._lock_item(reservation.product_id, reservation.location_id)
            item.reserved_quantity -= reservation.quantity
            item.available_quantity += reservation.quantity
            reservation.status = ReservationStatus.RELEASED.value
            units += reservation.quantity
            self.session.flush()
            self._record_movement(
                item,
                MovementType.RELEASE,
                reservation.quantity,
                request.order_id,
                request.reason,
            )

        logger.info(
            "inventory released",
            extra={
                "order_id": str(request.order_id),
                "units": units,
                "reason": request.reason,
            },
        )
        return len(reservations), units, False

    def commit(self, order_id: uuid.UUID) -> tuple[int, int, bool]:
        """Consume reserved stock permanently once the order ships.

        Reserved units leave the building. `available` is untouched -- those
        units were already moved out of it at reservation time.
        """
        reservations = self.session.scalars(
            select(Reservation)
            .where(
                Reservation.order_id == order_id,
                Reservation.status == ReservationStatus.HELD.value,
            )
            .order_by(Reservation.reservation_id)
            .with_for_update()
        ).all()

        if not reservations:
            already = self.session.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(
                    Reservation.order_id == order_id,
                    Reservation.status == ReservationStatus.COMMITTED.value,
                )
            )
            if already:
                return 0, 0, True
            raise NotFoundError(
                "No held reservation exists for that order.",
                details={"order_id": str(order_id)},
            )

        units = 0
        for reservation in reservations:
            item = self._lock_item(reservation.product_id, reservation.location_id)
            item.reserved_quantity -= reservation.quantity
            reservation.status = ReservationStatus.COMMITTED.value
            units += reservation.quantity
            self.session.flush()
            self._record_movement(
                item, MovementType.COMMIT, -reservation.quantity, order_id, "SHIPPED"
            )

        return len(reservations), units, False

    # ------------------------------------------------------------------
    # Restock
    # ------------------------------------------------------------------
    def restock(self, request: RestockRequest) -> InventoryItem:
        """Add units, creating the (product, location) row on first delivery."""
        self.get_location(request.location_id)

        item = self.session.scalar(
            select(InventoryItem)
            .where(
                InventoryItem.product_id == request.product_id,
                InventoryItem.location_id == request.location_id,
            )
            .with_for_update()
        )

        if item is None:
            item = InventoryItem(
                product_id=request.product_id,
                location_id=request.location_id,
                available_quantity=0,
                reserved_quantity=0,
                reorder_threshold=request.reorder_threshold or 0,
            )
            self.session.add(item)
            try:
                self.session.flush()
            except IntegrityError as exc:
                # Two first-deliveries racing; the unique constraint decides.
                self.session.rollback()
                raise ConflictError(
                    "Inventory record was created concurrently; retry the restock.",
                    details={
                        "product_id": str(request.product_id),
                        "location_id": str(request.location_id),
                    },
                ) from exc

        item.available_quantity += request.quantity
        if request.reorder_threshold is not None:
            item.reorder_threshold = request.reorder_threshold

        self.session.flush()
        self.session.refresh(item)
        self._record_movement(
            item, MovementType.RESTOCK, request.quantity, None, request.note
        )
        logger.info(
            "inventory restocked",
            extra={
                "product_id": str(request.product_id),
                "location_id": str(request.location_id),
                "quantity": request.quantity,
            },
        )
        return item

    def adjust(
        self, product_id: uuid.UUID, location_id: uuid.UUID, delta: int, note: str
    ) -> InventoryItem:
        """Manual correction after a stock count. Cannot drive available below zero."""
        if delta == 0:
            raise ValidationError("Adjustment delta must be non-zero.")

        item = self._lock_item(product_id, location_id)
        if item.available_quantity + delta < 0:
            raise ValidationError(
                "Adjustment would make available stock negative.",
                details={"available": item.available_quantity, "delta": delta},
            )

        item.available_quantity += delta
        self.session.flush()
        self._record_movement(item, MovementType.ADJUSTMENT, delta, None, note)
        return item
