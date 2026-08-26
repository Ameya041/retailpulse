"""Fulfilment persistence model.

Fulfilment is the point where the system stops being reversible. Up to
CONFIRMED an order is rows in databases; once a shipment leaves a warehouse
there is a physical object in a van. That asymmetry is why the fulfilment
state machine has no cancellation edge -- undoing a shipment is a returns
process, not a status change.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from retailpulse_common.db import Base

# Registers `processed_events` and `outbox_events` on this service's metadata.
from retailpulse_common.events.idempotency import ProcessedEvent  # noqa: F401,E402
from retailpulse_common.events.outbox import OutboxEvent  # noqa: F401,E402


class FulfilmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PICKING = "PICKING"
    PACKED = "PACKED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    FAILED_DELIVERY = "FAILED_DELIVERY"


#: Legal fulfilment transitions. Note there is no path to a cancelled state:
#: once picking has begun the goods are moving.
ALLOWED_FULFILMENT_TRANSITIONS: dict[FulfilmentStatus, frozenset[FulfilmentStatus]] = {
    FulfilmentStatus.PENDING: frozenset({FulfilmentStatus.PICKING}),
    FulfilmentStatus.PICKING: frozenset({FulfilmentStatus.PACKED}),
    FulfilmentStatus.PACKED: frozenset({FulfilmentStatus.SHIPPED}),
    FulfilmentStatus.SHIPPED: frozenset(
        {FulfilmentStatus.DELIVERED, FulfilmentStatus.FAILED_DELIVERY}
    ),
    FulfilmentStatus.DELIVERED: frozenset(),
    # A failed delivery is re-attempted: the parcel goes back out for delivery.
    FulfilmentStatus.FAILED_DELIVERY: frozenset({FulfilmentStatus.SHIPPED}),
}

_STATUS_SQL = ", ".join(f"'{s.value}'" for s in FulfilmentStatus)

CARRIERS = ("BLUEDART", "DELHIVERY", "DTDC", "EKART", "INDIA_POST")


class Fulfilment(Base):
    __tablename__ = "fulfilments"

    fulfilment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # One fulfilment per order. A redelivered ORDER_CONFIRMED must not create a
    # second shipment for the same goods.
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FulfilmentStatus.PENDING.value
    )
    carrier: Mapped[str | None] = mapped_column(String(24))
    tracking_number: Mapped[str | None] = mapped_column(String(48), unique=True)
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(120))

    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_fulfilments_status_valid"),
        CheckConstraint("delivery_attempts >= 0", name="ck_fulfilments_attempts_non_negative"),
        # A shipped parcel must have something to track it by. Enforced in the
        # database because a shipment with no tracking number is unfindable.
        CheckConstraint(
            "status NOT IN ('SHIPPED', 'DELIVERED', 'FAILED_DELIVERY') "
            "OR (tracking_number IS NOT NULL AND carrier IS NOT NULL)",
            name="ck_fulfilments_shipped_has_tracking",
        ),
        Index("ix_fulfilments_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Fulfilment {self.order_id} {self.status}>"
