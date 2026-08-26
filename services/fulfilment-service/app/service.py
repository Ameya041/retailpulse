"""Fulfilment business logic."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ALLOWED_FULFILMENT_TRANSITIONS,
    CARRIERS,
    Fulfilment,
    FulfilmentStatus,
)
from retailpulse_common.errors import ConflictError, NotFoundError

logger = logging.getLogger("fulfilment-service")

# Nominal transit time used for the customer-facing estimate.
ESTIMATED_TRANSIT_DAYS = 4
MAX_DELIVERY_ATTEMPTS = 3


class InvalidFulfilmentTransition(ConflictError):
    code = "invalid_fulfilment_transition"


def _tracking_number(carrier: str, rng: random.Random) -> str:
    """Carrier-style tracking number. Prefixed so support can tell them apart."""
    return f"{carrier[:3].upper()}{rng.randrange(10**11, 10**12)}"


class FulfilmentService:
    def __init__(self, session: Session, rng: random.Random | None = None) -> None:
        self.session = session
        # Injectable so tests get deterministic carriers and tracking numbers.
        self._rng = rng or random.Random()  # noqa: S311 - simulation, not crypto

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, order_id: uuid.UUID) -> Fulfilment:
        fulfilment = self.session.scalar(
            select(Fulfilment).where(Fulfilment.order_id == order_id)
        )
        if fulfilment is None:
            raise NotFoundError(
                f"No fulfilment exists for order {order_id}.",
                details={"order_id": str(order_id)},
            )
        return fulfilment

    def find(self, order_id: uuid.UUID) -> Fulfilment | None:
        return self.session.scalar(select(Fulfilment).where(Fulfilment.order_id == order_id))

    def list_fulfilments(
        self, *, status: FulfilmentStatus | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[Fulfilment], int]:
        base = select(Fulfilment)
        if status is not None:
            base = base.where(Fulfilment.status == status.value)
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.order_by(Fulfilment.created_at.desc(), Fulfilment.fulfilment_id)
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), total

    def estimated_delivery(self, fulfilment: Fulfilment) -> datetime | None:
        if fulfilment.delivered_at is not None:
            return fulfilment.delivered_at
        if fulfilment.shipped_at is None:
            return None
        return fulfilment.shipped_at + timedelta(days=ESTIMATED_TRANSIT_DAYS)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def _transition(self, fulfilment: Fulfilment, to_status: FulfilmentStatus) -> None:
        current = FulfilmentStatus(fulfilment.status)
        if current is to_status:
            return  # idempotent no-op, same as the order state machine
        if to_status not in ALLOWED_FULFILMENT_TRANSITIONS[current]:
            allowed = sorted(s.value for s in ALLOWED_FULFILMENT_TRANSITIONS[current])
            raise InvalidFulfilmentTransition(
                f"Cannot move a fulfilment from {current.value} to {to_status.value}.",
                details={
                    "order_id": str(fulfilment.order_id),
                    "current_status": current.value,
                    "requested_status": to_status.value,
                    "allowed_next": allowed or ["(none - terminal)"],
                },
            )
        fulfilment.status = to_status.value

    def start(
        self,
        order_id: uuid.UUID,
        shipping_address: str,
        *,
        customer_id: uuid.UUID | None = None,
    ) -> Fulfilment:
        """Create the fulfilment record for a confirmed order.

        Idempotent: an order that already has a fulfilment returns it
        unchanged, so a redelivered ORDER_CONFIRMED cannot create a second
        shipment for the same goods.
        """
        existing = self.find(order_id)
        if existing is not None:
            logger.info(
                "fulfilment already exists", extra={"order_id": str(order_id)}
            )
            return existing

        fulfilment = Fulfilment(
            order_id=order_id,
            customer_id=customer_id,
            shipping_address=shipping_address,
            status=FulfilmentStatus.PENDING.value,
        )
        self.session.add(fulfilment)
        try:
            self.session.flush()
        except IntegrityError:
            # Concurrent consumers raced; the unique index on order_id decided.
            self.session.rollback()
            winner = self.find(order_id)
            if winner is None:  # pragma: no cover - only on a genuine DB fault
                raise
            return winner

        logger.info("fulfilment started", extra={"order_id": str(order_id)})
        return fulfilment

    def begin_picking(self, order_id: uuid.UUID) -> Fulfilment:
        fulfilment = self.get(order_id)
        self._transition(fulfilment, FulfilmentStatus.PICKING)
        self.session.flush()
        return fulfilment

    def mark_packed(self, order_id: uuid.UUID) -> Fulfilment:
        fulfilment = self.get(order_id)
        self._transition(fulfilment, FulfilmentStatus.PACKED)
        self.session.flush()
        return fulfilment

    def ship(self, order_id: uuid.UUID, *, carrier: str | None = None) -> Fulfilment:
        """Hand the parcel to a carrier and assign tracking."""
        fulfilment = self.get(order_id)

        if FulfilmentStatus(fulfilment.status) is FulfilmentStatus.SHIPPED:
            return fulfilment  # idempotent

        # Re-shipping after a failed delivery keeps the original tracking
        # number: it is the same physical parcel, and a customer watching the
        # old number must not lose sight of it.
        redelivery = FulfilmentStatus(fulfilment.status) is FulfilmentStatus.FAILED_DELIVERY

        self._transition(fulfilment, FulfilmentStatus.SHIPPED)

        if not redelivery:
            fulfilment.carrier = (carrier or self._rng.choice(CARRIERS)).upper()
            fulfilment.tracking_number = _tracking_number(fulfilment.carrier, self._rng)
            fulfilment.shipped_at = datetime.now(UTC)

        fulfilment.failure_reason = None
        self.session.flush()

        logger.info(
            "shipment dispatched",
            extra={
                "order_id": str(order_id),
                "carrier": fulfilment.carrier,
                "tracking_number": fulfilment.tracking_number,
                "redelivery": redelivery,
            },
        )
        return fulfilment

    def deliver(self, order_id: uuid.UUID) -> Fulfilment:
        fulfilment = self.get(order_id)

        if FulfilmentStatus(fulfilment.status) is FulfilmentStatus.DELIVERED:
            return fulfilment  # idempotent

        self._transition(fulfilment, FulfilmentStatus.DELIVERED)
        fulfilment.delivered_at = datetime.now(UTC)
        fulfilment.delivery_attempts += 1
        self.session.flush()

        logger.info("delivery completed", extra={"order_id": str(order_id)})
        return fulfilment

    def fail_delivery(self, order_id: uuid.UUID, reason: str) -> Fulfilment:
        """Record a failed delivery attempt.

        After MAX_DELIVERY_ATTEMPTS the parcel stays in FAILED_DELIVERY rather
        than being retried forever -- at that point it needs a human, not
        another van.
        """
        fulfilment = self.get(order_id)
        self._transition(fulfilment, FulfilmentStatus.FAILED_DELIVERY)
        fulfilment.delivery_attempts += 1
        fulfilment.failure_reason = reason
        self.session.flush()

        logger.warning(
            "delivery attempt failed",
            extra={
                "order_id": str(order_id),
                "attempt": fulfilment.delivery_attempts,
                "reason": reason,
            },
        )
        return fulfilment

    def can_reattempt(self, fulfilment: Fulfilment) -> bool:
        return fulfilment.delivery_attempts < MAX_DELIVERY_ATTEMPTS
