"""Payment business logic."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateway import PaymentGateway
from app.models import ALLOWED_PAYMENT_TRANSITIONS, Payment, PaymentStatus
from retailpulse_common.errors import ConflictError, NotFoundError, ValidationError

logger = logging.getLogger("payment-service")


class PaymentService:
    def __init__(self, session: Session, gateway: PaymentGateway) -> None:
        self.session = session
        self.gateway = gateway

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_by_order(self, order_id: uuid.UUID) -> Payment:
        payment = self.session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            raise NotFoundError(
                f"No payment exists for order {order_id}.",
                details={"order_id": str(order_id)},
            )
        return payment

    def find_by_order(self, order_id: uuid.UUID) -> Payment | None:
        return self.session.scalar(select(Payment).where(Payment.order_id == order_id))

    def get_by_reference(self, reference: str) -> Payment:
        payment = self.session.scalar(
            select(Payment).where(Payment.transaction_reference == reference.upper())
        )
        if payment is None:
            raise NotFoundError(
                f"No payment with reference {reference}.", details={"reference": reference}
            )
        return payment

    def list_payments(
        self, *, status: PaymentStatus | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[Payment], int]:
        base = select(Payment)
        if status is not None:
            base = base.where(Payment.status == status.value)
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.order_by(Payment.created_at.desc(), Payment.payment_id)
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), total

    # ------------------------------------------------------------------
    # Charging
    # ------------------------------------------------------------------
    def charge(
        self,
        *,
        order_id: uuid.UUID,
        amount: Decimal,
        currency: str = "INR",
        customer_id: uuid.UUID | None = None,
        payment_method: str = "CARD",
    ) -> Payment:
        """Attempt to take payment for an order.

        Returns the resulting Payment whether it was approved or declined --
        a decline is an outcome, not an exception. Only an unreachable provider
        raises, because that is the case worth retrying.

        Idempotent: if this order already has a payment, that payment is
        returned untouched. This is what stops a redelivered PAYMENT_REQUESTED
        from charging twice.
        """
        existing = self.find_by_order(order_id)
        if existing is not None:
            logger.info(
                "charge skipped; order already has a payment",
                extra={"order_id": str(order_id), "status": existing.status},
            )
            return existing

        if amount <= 0:
            raise ValidationError(
                "Payment amount must be positive.", details={"amount": str(amount)}
            )

        # The gateway call happens before the insert. If the provider is
        # unreachable this raises and nothing is written, so the event is
        # retried cleanly with no orphan PENDING row to reconcile.
        result = self.gateway.charge(
            order_id=order_id, amount=amount, currency=currency, method=payment_method
        )

        payment = Payment(
            order_id=order_id,
            customer_id=customer_id,
            amount=amount,
            currency=currency.upper(),
            status=(
                PaymentStatus.SUCCESS.value if result.approved else PaymentStatus.FAILED.value
            ),
            payment_method=payment_method,
            transaction_reference=result.transaction_reference,
            failure_reason=result.failure_reason,
        )
        self.session.add(payment)

        try:
            self.session.flush()
        except IntegrityError:
            # Two consumers raced on the same order. The unique index on
            # order_id decided; return the winner rather than failing, because
            # the caller's intent (this order is paid) has been satisfied.
            self.session.rollback()
            winner = self.find_by_order(order_id)
            if winner is None:  # pragma: no cover - only on a genuine DB fault
                raise
            logger.warning(
                "concurrent charge collapsed onto the existing payment",
                extra={"order_id": str(order_id)},
            )
            return winner

        logger.info(
            "payment recorded",
            extra={
                "order_id": str(order_id),
                "status": payment.status,
                "reference": payment.transaction_reference,
            },
        )
        return payment

    # ------------------------------------------------------------------
    # Refunds
    # ------------------------------------------------------------------
    def refund(
        self, order_id: uuid.UUID, *, amount: Decimal | None = None, reason: str
    ) -> Payment:
        """Refund a successful payment, in full or in part."""
        payment = self.get_by_order(order_id)
        current = PaymentStatus(payment.status)

        if PaymentStatus.REFUNDED not in ALLOWED_PAYMENT_TRANSITIONS[current]:
            raise ConflictError(
                f"A payment in state {current.value} cannot be refunded.",
                details={
                    "order_id": str(order_id),
                    "status": current.value,
                    "allowed_next": sorted(
                        s.value for s in ALLOWED_PAYMENT_TRANSITIONS[current]
                    ),
                },
            )

        refund_amount = amount if amount is not None else payment.amount
        if refund_amount > payment.amount:
            raise ValidationError(
                "Refund cannot exceed the amount charged.",
                details={"charged": str(payment.amount), "requested": str(refund_amount)},
            )

        payment.status = PaymentStatus.REFUNDED.value
        payment.refunded_amount = refund_amount
        payment.notes = reason
        self.session.flush()

        logger.info(
            "payment refunded",
            extra={
                "order_id": str(order_id),
                "amount": str(refund_amount),
                "reason": reason,
            },
        )
        return payment

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        counts = dict(
            self.session.execute(
                select(Payment.status, func.count()).group_by(Payment.status)
            ).all()
        )
        total = sum(counts.values())
        successful = counts.get(PaymentStatus.SUCCESS.value, 0)
        refunded = counts.get(PaymentStatus.REFUNDED.value, 0)

        collected = self.session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.SUCCESS.value
            )
        ) or Decimal("0")
        refunded_total = self.session.scalar(
            select(func.coalesce(func.sum(Payment.refunded_amount), 0)).where(
                Payment.status == PaymentStatus.REFUNDED.value
            )
        ) or Decimal("0")

        # A refunded payment was a successful charge, so it counts towards the
        # approval rate. Excluding it would understate how the gateway performs.
        approved = successful + refunded
        return {
            "total_payments": total,
            "successful": successful,
            "failed": counts.get(PaymentStatus.FAILED.value, 0),
            "refunded": refunded,
            "pending": counts.get(PaymentStatus.PENDING.value, 0),
            "success_rate": round(approved / total, 4) if total else 0.0,
            "total_collected": Decimal(str(collected)),
            "total_refunded": Decimal(str(refunded_total)),
            "currency": "INR",
        }
