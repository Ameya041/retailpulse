"""Fulfilment event handlers.

Final step of the saga:

    order.confirmed -> create fulfilment -> fulfilment.started

Shipping and delivery are then driven by warehouse staff through the REST API,
each transition publishing an event the order service consumes to advance the
order to SHIPPED and finally DELIVERED.
"""

from __future__ import annotations

import logging
import random
import uuid

from sqlalchemy.orm import Session

from app.models import FulfilmentStatus
from app.service import FulfilmentService
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.outbox import enqueue
from retailpulse_common.events.producer import order_key
from retailpulse_common.events.topics import EventType, Topic

logger = logging.getLogger("fulfilment-service")

SERVICE = "fulfilment-service"
CONSUMER_GROUP = "fulfilment-service"


def _order_id(event: EventEnvelope) -> uuid.UUID:
    raw = event.payload.get("order_id")
    if raw is None:
        raise PermanentEventError("Event payload is missing 'order_id'.")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentEventError("'order_id' is not a valid UUID.") from exc


def handle_order_confirmed(
    event: EventEnvelope, topic: str, *, session: Session, rng: random.Random
) -> None:
    """Open a fulfilment for a confirmed order and start picking."""
    order_id = _order_id(event)

    address = event.payload.get("shipping_address")
    if not address:
        # Without an address there is nothing to fulfil, and no retry will
        # produce one.
        raise PermanentEventError("Event payload is missing 'shipping_address'.")

    raw_customer = event.payload.get("customer_id")
    customer_id = uuid.UUID(str(raw_customer)) if raw_customer else None

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    service = FulfilmentService(session, rng)
    fulfilment = service.start(order_id, str(address), customer_id=customer_id)

    # Move straight to PICKING: a confirmed order is work for the warehouse
    # immediately, and PENDING with nothing scheduling it is a state orders
    # would sit in forever.
    if FulfilmentStatus(fulfilment.status) is FulfilmentStatus.PENDING:
        service.begin_picking(order_id)

    enqueue(
        session,
        Topic.FULFILMENT_STARTED,
        event.child(
            event_type=EventType.FULFILMENT_STARTED,
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "fulfilment_id": str(fulfilment.fulfilment_id),
                "status": fulfilment.status,
            },
        ),
        key=order_key(order_id),
    )

    logger.info("fulfilment opened", extra={"order_id": str(order_id)})


HANDLERS = {
    Topic.ORDER_CONFIRMED: handle_order_confirmed,
}
