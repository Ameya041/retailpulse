"""Multi-location inventory persistence model.

The central invariant of this service:

    available_quantity >= 0  AND  reserved_quantity >= 0

A reservation moves units from `available` to `reserved`; it never creates or
destroys them. That is why both columns carry CHECK constraints -- the database
refuses to store negative stock even if a bug in the service layer tries.

Concurrency is the hard part. Two customers can buy the last unit at the same
millisecond. The correctness argument lives in `service.py` (row-level locking),
but the constraints here are the backstop: if the locking were ever wrong, the
transaction aborts instead of silently overselling.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from retailpulse_common.db import Base


class ReservationStatus(str, enum.Enum):
    """Lifecycle of a single reservation record."""

    HELD = "HELD"          # units moved available -> reserved
    RELEASED = "RELEASED"  # order cancelled or payment failed; units returned
    COMMITTED = "COMMITTED"  # order shipped; units permanently leave the location


class MovementType(str, enum.Enum):
    """Every quantity change is recorded, so stock is auditable."""

    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    COMMIT = "COMMIT"
    RESTOCK = "RESTOCK"
    ADJUSTMENT = "ADJUSTMENT"


class Location(Base):
    """A physical store or warehouse holding stock."""

    __tablename__ = "locations"

    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    inventory: Mapped[list[InventoryItem]] = relationship(back_populates="location")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Location {self.code}>"


class InventoryItem(Base):
    """Stock of one product at one location."""

    __tablename__ = "inventory"

    inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # No foreign key to products: that table lives in another service's
    # database. Referential integrity across a service boundary is the
    # application's job, enforced by validating the product exists before the
    # first restock.
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("locations.location_id", ondelete="RESTRICT"), nullable=False
    )
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reorder_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Deliberately NOT lazy="joined". An eager join would add a LEFT OUTER JOIN
    # to every query against this table, including the `SELECT ... FOR UPDATE`
    # in the reservation path -- and Postgres rejects FOR UPDATE on the nullable
    # side of an outer join. Read paths that need the location opt in with an
    # explicit joinedload instead.
    location: Mapped[Location] = relationship(back_populates="inventory")

    __table_args__ = (
        # One row per (product, location). Without this, a race could create
        # two rows for the same pair and stock would be split invisibly.
        UniqueConstraint("product_id", "location_id", name="uq_inventory_product_location"),
        CheckConstraint("available_quantity >= 0", name="ck_inventory_available_non_negative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_inventory_reserved_non_negative"),
        CheckConstraint("reorder_threshold >= 0", name="ck_inventory_threshold_non_negative"),
        # "How much of product X is there, everywhere?" is the hottest query.
        Index("ix_inventory_product_id", "product_id"),
        Index("ix_inventory_location_id", "location_id"),
    )

    @property
    def total_quantity(self) -> int:
        """Physically present units: still on the shelf plus held for orders."""
        return self.available_quantity + self.reserved_quantity

    @property
    def is_low(self) -> bool:
        return self.available_quantity <= self.reorder_threshold

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InventoryItem product={self.product_id} "
            f"available={self.available_quantity} reserved={self.reserved_quantity}>"
        )


class Reservation(Base):
    """A hold placed on stock for a specific order.

    Reservations are tracked as rows rather than only as a counter so that a
    release can be tied to the exact order that reserved the units, and so a
    duplicate reserve for the same order is detectable.
    """

    __tablename__ = "reservations"

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("locations.location_id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReservationStatus.HELD.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # The idempotency guarantee for reservations: replaying ORDER_CREATED
        # cannot hold the same product for the same order twice. This unique
        # index is the enforcement -- not an application-level "did we already
        # do this?" check, which races.
        UniqueConstraint(
            "order_id", "product_id", "location_id", name="uq_reservation_order_product_location"
        ),
        CheckConstraint("quantity > 0", name="ck_reservations_quantity_positive"),
        CheckConstraint(
            "status IN ('HELD', 'RELEASED', 'COMMITTED')", name="ck_reservations_status_valid"
        ),
        Index("ix_reservations_order_id", "order_id"),
        Index("ix_reservations_status", "status"),
    )


class StockMovement(Base):
    """Append-only ledger of every quantity change.

    Current quantities in `inventory` are a running total; this table is the
    history that explains how they got there. When stock looks wrong in
    production, this is the table that answers "what happened?".
    """

    __tablename__ = "stock_movements"

    movement_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    available_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # order or reservation
    note: Mapped[str | None] = mapped_column(Text)
    # Python-side timestamp, not server_default=now(). In Postgres `now()`
    # returns the *transaction* start time, so every movement written inside one
    # transaction -- a reserve that spans three locations, say -- would carry an
    # identical timestamp and the ledger could not be ordered. A microsecond
    # clock reading per row keeps the append order recoverable.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('RESERVE', 'RELEASE', 'COMMIT', 'RESTOCK', 'ADJUSTMENT')",
            name="ck_stock_movements_type_valid",
        ),
        Index("ix_stock_movements_product_created", "product_id", "created_at"),
        Index("ix_stock_movements_reference_id", "reference_id"),
    )
