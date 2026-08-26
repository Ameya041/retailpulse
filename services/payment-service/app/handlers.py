"""Payment event handlers.

Third step of the saga:

    payment.requested  ->  charge  ->  payment.confirmed
                                   \\-> payment.failed

The distinction this module exists to get right: **a decline is not an error.**

* A *decline* is the provider answering "no". It is a business outcome. It must
  be published as PAYMENT_FAILED, the event acknowledged, and the offset
  committed. Retrying it would charge nothing and delay the saga.
* An *outage* is the provider not answering at all. That is a technical
  failure, so it propagates and the consumer retries with backoff.

Conflating them produces one of two bad systems: one that retries declines
forever, or one that treats an outage as a customer's card being refused.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.gateway import PaymentGateway
from app.models import PaymentStatus
from app.service import PaymentService
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.outbox import enqueue
from retailpulse_common.events.producer import order_key
from retailpulse_common.events.topics import EventType, Topic

logger = logging.getLogger("payment-service")

SERVICE = "payment-service"
CONSUMER_GROUP = "payment-service"


def _order_id(event: EventEnvelope) -> uuid.UUID:
    raw = event.payload.get("order_id")
    if raw is None:
        raise PermanentEventError("Event payload is missing 'order_id'.")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentEventError("'order_id' is not a valid UUID.") from exc


def _amount(event: EventEnvelope) -> Decimal:
    raw = event.payload.get("amount")
    if raw is None:
        raise PermanentEventError("Event payload is missing 'amount'.")
    try:
        # Parsed from the string on the wire. Going via float here would
        # reintroduce exactly the rounding error NUMERIC exists to prevent.
        amount = Decimal(str(raw))
    except InvalidOperation as exc:
        raise PermanentEventError(f"'amount' is not a valid decimal: {raw!r}") from exc
    if amount <= 0:
        raise PermanentEventError(f"'amount' must be positive, got {amount}.")
    return amount


def handle_payment_requested(
    event: EventEnvelope, topic: str, *, session: Session, gateway: PaymentGateway
) -> None:
    """Charge for an order and publish the outcome."""
    order_id = _order_id(event)
    amount = _amount(event)
    currency = str(event.payload.get("currency", "INR"))
    raw_customer = event.payload.get("customer_id")
    customer_id = uuid.UUID(str(raw_customer)) if raw_customer else None

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    # A provider outage raises out of here and the consumer retries it. Note
    # nothing has been written yet, so a retry starts from a clean slate.
    payment = PaymentService(session, gateway).charge(
        order_id=order_id,
        amount=amount,
        currency=currency,
        customer_id=customer_id,
    )

    approved = PaymentStatus(payment.status) in (
        PaymentStatus.SUCCESS,
        PaymentStatus.REFUNDED,
    )

    # Staged in the outbox, not published directly: the charge and the event
    # announcing it must commit together, or a customer is charged with nobody
    # downstream ever finding out.
    enqueue(
        session,
        Topic.PAYMENT_CONFIRMED if approved else Topic.PAYMENT_FAILED,
        event.child(
            event_type=(
                EventType.PAYMENT_CONFIRMED if approved else EventType.PAYMENT_FAILED
            ),
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "payment_id": str(payment.payment_id),
                "amount": str(payment.amount),
                "currency": payment.currency,
                "transaction_reference": payment.transaction_reference,
                "reason": payment.failure_reason,
            },
        ),
        key=order_key(order_id),
    )

    logger.info(
        "payment outcome published",
        extra={
            "order_id": str(order_id),
            "approved": approved,
            "reason": payment.failure_reason,
        },
    )


def handle_order_cancelled(
    event: EventEnvelope, topic: str, *, session: Session, gateway: PaymentGateway
) -> None:
    """Refund automatically if a paid order is later cancelled.

    Orders cancelled before payment have nothing to refund, which is the common
    case and a silent no-op.
    """
    order_id = _order_id(event)

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    service = PaymentService(session, gateway)
    payment = service.find_by_order(order_id)
    if payment is None or PaymentStatus(payment.status) is not PaymentStatus.SUCCESS:
        logger.info(
            "cancellation needs no refund",
            extra={
                "order_id": str(order_id),
                "status": payment.status if payment else "NO_PAYMENT",
            },
        )
        return

    service.refund(
        order_id,
        reason=str(event.payload.get("reason", "ORDER_CANCELLED"))[:120],
    )
    logger.info("auto-refund issued", extra={"order_id": str(order_id)})


HANDLERS = {
    Topic.PAYMENT_REQUESTED: handle_payment_requested,
    Topic.ORDER_CANCELLED: handle_order_cancelled,
}
