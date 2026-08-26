"""Event handlers for the order service.

The order service both starts the saga and reacts to its outcome:

    POST /orders          -> publish order.created
    inventory.reserved    -> status INVENTORY_RESERVED
    inventory.failed      -> status CANCELLED
    payment.confirmed     -> PAYMENT_CONFIRMED -> CONFIRMED
    payment.failed        -> PAYMENT_FAILED
    inventory.released    -> INVENTORY_RELEASED -> CANCELLED

Every handler is a thin translation from event to state transition. All the
rules about which transitions are legal live in ``order_state.py``, so a
malformed or out-of-order event cannot corrupt an order's status -- the worst
it can do is be rejected.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.order_state import OrderStatus
from app.service import OrderService, order_created_payload  # noqa: F401
from retailpulse_common.errors import ConflictError, NotFoundError
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.outbox import enqueue
from retailpulse_common.events.producer import EventPublisher, order_key
from retailpulse_common.events.topics import EventType, Topic

logger = logging.getLogger("order-service")

SERVICE = "order-service"
CONSUMER_GROUP = "order-service"


def _order_id(event: EventEnvelope) -> uuid.UUID:
    raw = event.payload.get("order_id")
    if raw is None:
        raise PermanentEventError("Event payload is missing 'order_id'.")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentEventError("'order_id' is not a valid UUID.") from exc


def _apply(
    event: EventEnvelope,
    topic: str,
    session: Session,
    to_status: OrderStatus,
    *,
    reason: str | None = None,
) -> None:
    """Claim the event and move the order, in one transaction."""
    order_id = _order_id(event)

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    service = OrderService(session, catalog=_NullCatalog())
    try:
        service.transition(order_id, to_status, actor=event.source, reason=reason)
    except NotFoundError as exc:
        # The order does not exist and never will -- events are keyed by
        # order_id and the order service is the only writer. Retrying is
        # pointless.
        raise PermanentEventError(f"Unknown order {order_id}.") from exc
    except ConflictError as exc:
        # An illegal transition. This means events arrived out of order or a
        # stale event was replayed after the order moved on. Retrying will not
        # help, and the state machine has already protected the order, so
        # record it and move on rather than dead-lettering a harmless event.
        logger.warning(
            "ignoring event that would make an illegal transition",
            extra={
                "order_id": str(order_id),
                "event_type": event.event_type,
                "requested_status": to_status.value,
                "detail": exc.details,
            },
        )


class _NullCatalog:
    """Handlers never price anything -- that happened at order creation."""

    def get_many(self, product_ids):  # noqa: ARG002
        return {}


def handle_inventory_reserved(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Stock is held. Advance the order and hand off to payment."""
    _apply(event, topic, session, OrderStatus.INVENTORY_RESERVED)

    order_id = _order_id(event)
    order = OrderService(session, _NullCatalog()).get(order_id)

    # Only ask for payment if the order really is in the reserved state --
    # a replayed or out-of-order event must not trigger a second charge.
    if OrderStatus(order.status) is not OrderStatus.INVENTORY_RESERVED:
        return

    enqueue(
        session,
        Topic.PAYMENT_REQUESTED,
        event.child(
            event_type=EventType.PAYMENT_REQUESTED,
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "customer_id": str(order.customer_id),
                "amount": str(order.total_amount),
                "currency": order.currency,
            },
        ),
        key=order_key(order_id),
    )


def handle_inventory_failed(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Stock could not be held, so the order cannot proceed."""
    reason = str(event.payload.get("reason", "INSUFFICIENT_INVENTORY"))[:120]
    _apply(event, topic, session, OrderStatus.CANCELLED, reason=reason)

    enqueue(
        session,
        Topic.ORDER_CANCELLED,
        event.child(
            event_type=EventType.ORDER_CANCELLED,
            source=SERVICE,
            payload={"order_id": str(_order_id(event)), "reason": reason},
        ),
        key=order_key(_order_id(event)),
    )


def handle_payment_confirmed(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Payment succeeded: PAYMENT_CONFIRMED then straight on to CONFIRMED."""
    _apply(event, topic, session, OrderStatus.PAYMENT_CONFIRMED)

    order_id = _order_id(event)
    service = OrderService(session, _NullCatalog())
    order = service.get(order_id)
    if OrderStatus(order.status) is not OrderStatus.PAYMENT_CONFIRMED:
        return

    service.transition(order_id, OrderStatus.CONFIRMED, actor=SERVICE)

    enqueue(
        session,
        Topic.ORDER_CONFIRMED,
        event.child(
            event_type=EventType.ORDER_CONFIRMED,
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "customer_id": str(order.customer_id),
                "total_amount": str(order.total_amount),
                "currency": order.currency,
                "shipping_address": order.shipping_address,
            },
        ),
        key=order_key(order_id),
    )


def handle_payment_failed(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Payment failed. The inventory service compensates on its own topic."""
    reason = str(event.payload.get("reason", "PAYMENT_FAILED"))[:120]
    _apply(event, topic, session, OrderStatus.PAYMENT_FAILED, reason=reason)


def handle_inventory_released(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Compensation finished: stock is back, so the order can be closed."""
    _apply(event, topic, session, OrderStatus.INVENTORY_RELEASED)

    order_id = _order_id(event)
    service = OrderService(session, _NullCatalog())
    order = service.get(order_id)
    if OrderStatus(order.status) is not OrderStatus.INVENTORY_RELEASED:
        return

    service.transition(
        order_id, OrderStatus.CANCELLED, actor=SERVICE, reason="PAYMENT_FAILED"
    )
    enqueue(
        session,
        Topic.ORDER_CANCELLED,
        event.child(
            event_type=EventType.ORDER_CANCELLED,
            source=SERVICE,
            payload={"order_id": str(order_id), "reason": "PAYMENT_FAILED"},
        ),
        key=order_key(order_id),
    )


def handle_fulfilment_started(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    _apply(event, topic, session, OrderStatus.FULFILMENT_STARTED)


def handle_order_shipped(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    _apply(event, topic, session, OrderStatus.SHIPPED)


def handle_order_delivered(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    _apply(event, topic, session, OrderStatus.DELIVERED)


#: Topic -> handler. The worker dispatches on this.
HANDLERS = {
    Topic.INVENTORY_RESERVED: handle_inventory_reserved,
    Topic.INVENTORY_FAILED: handle_inventory_failed,
    Topic.INVENTORY_RELEASED: handle_inventory_released,
    Topic.PAYMENT_CONFIRMED: handle_payment_confirmed,
    Topic.PAYMENT_FAILED: handle_payment_failed,
    Topic.FULFILMENT_STARTED: handle_fulfilment_started,
    Topic.ORDER_SHIPPED: handle_order_shipped,
    Topic.ORDER_DELIVERED: handle_order_delivered,
}
