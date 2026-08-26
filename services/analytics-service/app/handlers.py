"""Analytics event handlers.

The analytics service is a pure consumer: it publishes nothing and owns no
transactional state. It builds a read model from the events the other services
emit.

**Why a separate consumer group.** ORDER_CONFIRMED is consumed by both the
fulfilment service and this one, for entirely different reasons. Because
`processed_events` is keyed by `(event_id, consumer_group)`, neither
suppresses the other -- analytics recording an event has no bearing on whether
fulfilment has.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.service import AnalyticsService
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.topics import Topic

logger = logging.getLogger("analytics-service")

SERVICE = "analytics-service"
CONSUMER_GROUP = "analytics-service"


def _order_id(event: EventEnvelope) -> uuid.UUID:
    raw = event.payload.get("order_id")
    if raw is None:
        raise PermanentEventError("Event payload is missing 'order_id'.")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentEventError("'order_id' is not a valid UUID.") from exc


def _decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise PermanentEventError(f"{field!r} is not a valid decimal: {value!r}") from exc


def _event_date(event: EventEnvelope) -> date:
    """The business date is the event's own timestamp, not today.

    A replay or a late-arriving event must land on the day the sale actually
    happened; using the processing date would silently shift revenue between
    days and make every aggregate wrong in a way nobody would notice.
    """
    timestamp = event.timestamp
    if isinstance(timestamp, datetime):
        return timestamp.date()
    return date.today()  # pragma: no cover - envelope always carries a datetime


def handle_order_confirmed(event: EventEnvelope, topic: str, *, session: Session) -> None:
    """Record the sale. A confirmed order is a sale; a created one is not."""
    order_id = _order_id(event)

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    service = AnalyticsService(session)
    sale_date = _event_date(event)
    currency = str(event.payload.get("currency", "INR"))
    raw_customer = event.payload.get("customer_id")
    customer_id = uuid.UUID(str(raw_customer)) if raw_customer else None

    lines = event.payload.get("lines") or []
    facts = []
    for line in lines:
        try:
            quantity = int(line["quantity"])
            unit_price = _decimal(line["unit_price"], "unit_price")
            product_id = uuid.UUID(str(line["product_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentEventError(f"Malformed order line: {exc}") from exc

        facts.append(
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "product_id": product_id,
                "sku": str(line.get("sku", "UNKNOWN")),
                "product_name": str(line.get("product_name", line.get("sku", "Unknown"))),
                "category": str(line.get("category", "Uncategorised")),
                "store_id": str(line.get("store_id", "UNKNOWN")),
                "quantity": quantity,
                "unit_price": unit_price,
                "revenue": (unit_price * quantity).quantize(Decimal("0.01")),
                "currency": currency,
                "sale_date": sale_date,
            }
        )

    written = service.record_sale_lines(facts)

    service.record_order_event(
        order_id,
        "CONFIRMED",
        _decimal(event.payload.get("total_amount", 0), "total_amount"),
        currency,
        sale_date,
        None,
    )

    logger.info(
        "sale recorded",
        extra={"order_id": str(order_id), "lines": written, "sale_date": str(sale_date)},
    )


def _lifecycle_handler(status: str):
    """Build a handler that records one terminal order status."""

    def handle(event: EventEnvelope, topic: str, *, session: Session) -> None:
        order_id = _order_id(event)

        IdempotencyGuard(session, CONSUMER_GROUP).claim(
            event_id=event.event_id,
            event_type=event.event_type,
            topic=topic,
            correlation_id=event.correlation_id,
        )

        AnalyticsService(session).record_order_event(
            order_id,
            status,
            _decimal(event.payload.get("total_amount", 0), "total_amount"),
            str(event.payload.get("currency", "INR")),
            _event_date(event),
            str(event.payload.get("reason"))[:120] if event.payload.get("reason") else None,
        )
        logger.info(
            "order lifecycle recorded",
            extra={"order_id": str(order_id), "status": status},
        )

    return handle


handle_order_delivered = _lifecycle_handler("DELIVERED")
handle_order_cancelled = _lifecycle_handler("CANCELLED")
handle_order_shipped = _lifecycle_handler("SHIPPED")


HANDLERS = {
    Topic.ORDER_CONFIRMED: handle_order_confirmed,
    Topic.ORDER_DELIVERED: handle_order_delivered,
    Topic.ORDER_CANCELLED: handle_order_cancelled,
    Topic.ORDER_SHIPPED: handle_order_shipped,
}
