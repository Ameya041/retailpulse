"""Payment persistence model.

The single most important constraint in this service is the unique index on
``order_id``.

Payment is triggered by a Kafka event, and Kafka delivers at least once. A
redelivered PAYMENT_REQUESTED must never charge a customer twice. Two things
enforce that, in layers:

1. ``processed_events`` suppresses the duplicate event before any work starts.
2. The unique index on ``payments.order_id`` is the backstop -- even if the
   idempotency check were bypassed (a new consumer group, a manual replay, a
   bug), the database physically refuses a second charge for the same order.

Layer 2 matters because layer 1 is application logic and layer 2 is not. When
the failure mode is "customer charged twice", the guarantee belongs in the
database.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
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


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


#: Legal payment state transitions. Same reasoning as the order state machine:
#: a status is a node in a graph, not a string anyone may overwrite.
ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
    # Only a successful charge can be refunded.
    PaymentStatus.SUCCESS: frozenset({PaymentStatus.REFUNDED}),
    # A failed charge is terminal: retrying creates a new attempt, it does not
    # revive the old one.
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}

_STATUS_SQL = ", ".join(f"'{s.value}'" for s in PaymentStatus)


class Payment(Base):
    __tablename__ = "payments"

    payment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # No foreign key: orders live in another service's database. The unique
    # constraint is what guarantees one payment per order.
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaymentStatus.PENDING.value
    )
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False, default="CARD")
    # What a customer quotes at you on the phone. Unique so a reference can
    # never point at two payments.
    transaction_reference: Mapped[str] = mapped_column(
        String(48), nullable=False, unique=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(120))
    refunded_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)
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
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_payments_status_valid"),
        # Money never negative, and a refund never exceeds what was charged.
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "refunded_amount IS NULL OR (refunded_amount >= 0 AND refunded_amount <= amount)",
            name="ck_payments_refund_within_amount",
        ),
        CheckConstraint("length(currency) = 3", name="ck_payments_currency_iso"),
        Index("ix_payments_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Payment {self.transaction_reference} {self.status} {self.amount}>"
