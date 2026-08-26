"""Event handlers for the inventory service.

This is the second step of the order saga:

    order.created  ->  reserve stock  ->  inventory.reserved
                                      \\-> inventory.failed

**Why a saga rather than a distributed transaction.** Order state lives in one
database and stock in another. A two-phase commit across them would couple
their availability -- neither service could commit while the other was down --
and 2PC coordinators are their own operational burden. Instead each step
commits locally and publishes what happened; failure is handled by a
compensating action (release the stock) rather than by rolling back a
transaction that has already committed elsewhere.

**Atomicity that does still apply.** Claiming the event and reserving the
stock happen in *one* local transaction. Either both land or neither does,
so an event can never be marked processed while its effect was rolled back.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.schemas import ReleaseRequest, ReservationLine, ReserveRequest
from app.service import InventoryService
from retailpulse_common.errors import InsufficientInventoryError, NotFoundError
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.producer import EventPublisher, order_key
from retailpulse_common.events.topics import EventType, Topic

logger = logging.getLogger("inventory-service")

SERVICE = "inventory-service"
CONSUMER_GROUP = "inventory-service"


def _require(payload: dict, field: str):
    """Pull a required field, or fail permanently.

    A missing field will still be missing on every retry, so this is a
    PermanentEventError -- retrying would only delay every message queued
    behind it on the same partition.
    """
    value = payload.get(field)
    if value is None:
        raise PermanentEventError(f"Event payload is missing required field {field!r}.")
    return value


def _uuid(payload: dict, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(_require(payload, field)))
    except ValueError as exc:
        raise PermanentEventError(f"Field {field!r} is not a valid UUID.") from exc


def handle_order_created(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Reserve stock for a newly created order.

    Publishes ``inventory.reserved`` on success or ``inventory.failed`` when
    stock is insufficient. Both are normal business outcomes -- neither is an
    error the consumer should retry.
    """
    payload = event.payload
    order_id = _uuid(payload, "order_id")
    raw_lines = _require(payload, "lines")

    if not isinstance(raw_lines, list) or not raw_lines:
        raise PermanentEventError("Event payload has no order lines.")

    # Claim first, inside the caller's transaction. If this raises
    # DuplicateEventError the consumer treats the event as already handled.
    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    try:
        lines = [
            ReservationLine(
                product_id=uuid.UUID(str(line["product_id"])),
                quantity=int(line["quantity"]),
                location_id=(
                    uuid.UUID(str(line["location_id"])) if line.get("location_id") else None
                ),
            )
            for line in raw_lines
        ]
    except (KeyError, ValueError, TypeError) as exc:
        raise PermanentEventError(f"Malformed order line: {exc}") from exc

    service = InventoryService(session)

    try:
        allocations, replay = service.reserve(
            ReserveRequest(order_id=order_id, lines=lines)
        )
    except (InsufficientInventoryError, NotFoundError) as exc:
        # A business outcome, not a technical failure. The stock genuinely is
        # not there, and retrying will not conjure it. Tell the order service
        # so it can cancel, and treat the event as successfully handled.
        #
        # The claim above is committed by the caller alongside this publish,
        # so a redelivery will not re-emit the failure.
        logger.info(
            "reservation rejected",
            extra={"order_id": str(order_id), "reason": exc.code},
        )
        publisher.publish(
            Topic.INVENTORY_FAILED,
            event.child(
                event_type=EventType.INVENTORY_FAILED,
                source=SERVICE,
                payload={
                    "order_id": str(order_id),
                    "reason": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            ),
            key=order_key(order_id),
        )
        return

    publisher.publish(
        Topic.INVENTORY_RESERVED,
        event.child(
            event_type=EventType.INVENTORY_RESERVED,
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "idempotent_replay": replay,
                "allocations": [
                    {
                        "reservation_id": str(a.reservation_id),
                        "product_id": str(a.product_id),
                        "location_id": str(a.location_id),
                        "location_code": a.location_code,
                        "quantity": a.quantity,
                    }
                    for a in allocations
                ],
            },
        ),
        key=order_key(order_id),
    )

    _emit_low_stock_warnings(service, publisher, event, allocations)


def handle_payment_failed(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Compensating action: give the held stock back.

    This is the saga's rollback. The reservation already committed, so it
    cannot be undone by a database rollback -- it is undone by an explicit
    inverse operation.
    """
    order_id = _uuid(event.payload, "order_id")

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    service = InventoryService(session)
    try:
        lines, units, replay = service.release(
            ReleaseRequest(order_id=order_id, reason="PAYMENT_FAILED")
        )
    except NotFoundError:
        # Nothing was ever held for this order -- reservation must have failed
        # earlier. There is nothing to compensate, so this is a success.
        logger.info(
            "no reservation to release", extra={"order_id": str(order_id)}
        )
        lines, units, replay = 0, 0, True

    publisher.publish(
        Topic.INVENTORY_RELEASED,
        event.child(
            event_type=EventType.INVENTORY_RELEASED,
            source=SERVICE,
            payload={
                "order_id": str(order_id),
                "released_lines": lines,
                "released_units": units,
                "idempotent_replay": replay,
            },
        ),
        key=order_key(order_id),
    )


def handle_order_shipped(
    event: EventEnvelope, topic: str, *, session: Session, publisher: EventPublisher
) -> None:
    """Consume the reservation permanently once goods have shipped."""
    order_id = _uuid(event.payload, "order_id")

    IdempotencyGuard(session, CONSUMER_GROUP).claim(
        event_id=event.event_id,
        event_type=event.event_type,
        topic=topic,
        correlation_id=event.correlation_id,
    )

    try:
        InventoryService(session).commit(order_id)
    except NotFoundError:
        logger.warning(
            "shipped order had no held reservation", extra={"order_id": str(order_id)}
        )


def _emit_low_stock_warnings(
    service: InventoryService,
    publisher: EventPublisher,
    event: EventEnvelope,
    allocations,
) -> None:
    """Announce any location that dropped to or below its reorder threshold.

    Emitted as an event rather than written straight to a table so that
    analytics, replenishment and (later) notifications can all react without
    the inventory service knowing any of them exist.
    """
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for allocation in allocations:
        pair = (allocation.product_id, allocation.location_id)
        if pair in seen:
            continue
        seen.add(pair)

        try:
            item = service.get_item(allocation.product_id, allocation.location_id)
        except NotFoundError:  # pragma: no cover - defensive
            continue

        if not item.is_low:
            continue

        publisher.publish(
            Topic.INVENTORY_LOW,
            event.child(
                event_type=EventType.INVENTORY_LOW,
                source=SERVICE,
                payload={
                    "product_id": str(item.product_id),
                    "location_id": str(item.location_id),
                    "available_quantity": item.available_quantity,
                    "reorder_threshold": item.reorder_threshold,
                },
            ),
            key=str(item.product_id),
        )
