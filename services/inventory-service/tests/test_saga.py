"""Saga tests for the inventory service.

Covers the second step of the order flow: consuming ``order.created``,
reserving stock, and publishing the outcome -- plus the compensating release
when payment later fails.
"""

from __future__ import annotations

import uuid

import pytest

from app.handlers import (
    CONSUMER_GROUP,
    handle_order_created,
    handle_order_shipped,
    handle_payment_failed,
)
from app.schemas import RestockRequest
from app.service import InventoryService
from retailpulse_common.events.consumer import EventProcessor, PermanentEventError, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import DuplicateEventError, IdempotencyGuard
from retailpulse_common.events.outbox import OutboxRelay
from retailpulse_common.events.producer import InMemoryEventPublisher
from retailpulse_common.events.topics import DeadLetterTopic, EventType, Topic


@pytest.fixture()
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher(source="test")


def drain(database, publisher: InMemoryEventPublisher) -> InMemoryEventPublisher:
    """Run the outbox relay, then assert on what actually reached Kafka.

    Handlers stage events in the outbox rather than publishing inline, so these
    tests exercise the real production path -- handler -> outbox -> relay ->
    broker -- instead of stopping at the handler.
    """
    OutboxRelay(database, publisher).publish_pending()
    return publisher


def order_created(order_id, product_id, quantity, location_id=None) -> EventEnvelope:
    line = {"product_id": str(product_id), "quantity": quantity}
    if location_id is not None:
        line["location_id"] = str(location_id)
    return EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        source="order-service",
        payload={"order_id": str(order_id), "lines": [line]},
    )


def _stock(database, quantity=10, threshold=0):
    """Create a location with stock. Returns (location_id, product_id)."""
    product_id = uuid.uuid4()
    with database.session() as session:
        service = InventoryService(session)
        location = service.create_location(
            f"T{uuid.uuid4().hex[:5].upper()}", "Test Store", "Testville"
        )
        session.flush()
        location_id = location.location_id
        service.restock(
            RestockRequest(
                product_id=product_id,
                location_id=location_id,
                quantity=quantity,
                reorder_threshold=threshold,
            )
        )
    return location_id, product_id


# ---------------------------------------------------------------------------
# Reserving on ORDER_CREATED
# ---------------------------------------------------------------------------
def test_order_created_reserves_stock_and_publishes_reserved(database, publisher):
    location_id, product_id = _stock(database, 10)
    order_id = uuid.uuid4()
    event = order_created(order_id, product_id, 3, location_id)

    with database.session() as session:
        handle_order_created(event, Topic.ORDER_CREATED, session=session, publisher=publisher)

    with database.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.available_quantity == 7
        assert item.reserved_quantity == 3

    reserved = drain(database, publisher).only_event_on(Topic.INVENTORY_RESERVED)
    assert reserved.payload["order_id"] == str(order_id)
    assert reserved.payload["allocations"][0]["quantity"] == 3
    # Same saga chain as the originating event.
    assert reserved.correlation_id == event.correlation_id


def test_reserved_event_is_keyed_by_order_id(database, publisher):
    """Keeps every event for one order on the same partition, hence ordered."""
    location_id, product_id = _stock(database, 10)
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_order_created(
            order_created(order_id, product_id, 1, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    assert drain(database, publisher).keys_on(Topic.INVENTORY_RESERVED) == [str(order_id)]


def test_insufficient_stock_publishes_inventory_failed_not_an_exception(database, publisher):
    """Out of stock is a business outcome, not a retryable error."""
    location_id, product_id = _stock(database, 2)
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_order_created(
            order_created(order_id, product_id, 5, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    assert drain(database, publisher).topics() == [Topic.INVENTORY_FAILED]
    failed = drain(database, publisher).only_event_on(Topic.INVENTORY_FAILED)
    assert failed.payload["reason"] == "insufficient_inventory"
    assert failed.payload["details"]["available"] == 2

    # Stock untouched.
    with database.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.available_quantity == 2
        assert item.reserved_quantity == 0


def test_unknown_product_publishes_inventory_failed(database, publisher):
    with database.session() as session:
        handle_order_created(
            order_created(uuid.uuid4(), uuid.uuid4(), 1),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    assert drain(database, publisher).topics() == [Topic.INVENTORY_FAILED]


def test_low_stock_event_is_emitted_when_threshold_is_crossed(database, publisher):
    location_id, product_id = _stock(database, 10, threshold=8)

    with database.session() as session:
        handle_order_created(
            order_created(uuid.uuid4(), product_id, 3, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    assert Topic.INVENTORY_LOW in drain(database, publisher).topics()
    low = drain(database, publisher).only_event_on(Topic.INVENTORY_LOW)
    assert low.payload["available_quantity"] == 7
    assert low.payload["reorder_threshold"] == 8


def test_no_low_stock_event_when_above_threshold(database, publisher):
    location_id, product_id = _stock(database, 100, threshold=5)

    with database.session() as session:
        handle_order_created(
            order_created(uuid.uuid4(), product_id, 1, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    assert Topic.INVENTORY_LOW not in drain(database, publisher).topics()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_redelivered_order_created_does_not_reserve_twice(database, publisher):
    location_id, product_id = _stock(database, 10)
    event = order_created(uuid.uuid4(), product_id, 3, location_id)

    with database.session() as session:
        handle_order_created(event, Topic.ORDER_CREATED, session=session, publisher=publisher)

    with pytest.raises(DuplicateEventError), database.session() as session:
        handle_order_created(event, Topic.ORDER_CREATED, session=session, publisher=publisher)

    with database.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.available_quantity == 7  # not 4
        assert item.reserved_quantity == 3


def test_processor_commits_a_duplicate_rather_than_dead_lettering(database, publisher):
    location_id, product_id = _stock(database, 10)
    event = order_created(uuid.uuid4(), product_id, 2, location_id)

    def dispatch(evt, topic):
        with database.session() as session:
            handle_order_created(evt, topic, session=session, publisher=publisher)

    processor = EventProcessor(
        service_name="inventory-service",
        consumer_group=CONSUMER_GROUP,
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=lambda _: None,
    )

    assert processor.process(event, Topic.ORDER_CREATED, dispatch)
    assert processor.process(event, Topic.ORDER_CREATED, dispatch)

    assert DeadLetterTopic.INVENTORY not in drain(database, publisher).topics()
    assert len(drain(database, publisher).events_on(Topic.INVENTORY_RESERVED)) == 1
    with database.session() as session:
        assert InventoryService(session).get_item(product_id, location_id).reserved_quantity == 2


def test_claim_and_reservation_roll_back_together(database, publisher):
    """If the transaction fails, the event must look unprocessed again."""
    location_id, product_id = _stock(database, 10)
    event = order_created(uuid.uuid4(), product_id, 2, location_id)

    with pytest.raises(RuntimeError), database.session() as session:
        handle_order_created(
            event, Topic.ORDER_CREATED, session=session, publisher=publisher
        )
        raise RuntimeError("something failed after the handler")

    with database.session() as session:
        assert not IdempotencyGuard(session, CONSUMER_GROUP).has_processed(event.event_id)
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.available_quantity == 10  # reservation rolled back too
        assert item.reserved_quantity == 0


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------
def test_payment_failure_releases_the_held_stock(database, publisher):
    location_id, product_id = _stock(database, 10)
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_order_created(
            order_created(order_id, product_id, 4, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )

    with database.session() as session:
        handle_payment_failed(
            EventEnvelope(
                event_type=EventType.PAYMENT_FAILED,
                source="payment-service",
                payload={"order_id": str(order_id)},
            ),
            Topic.PAYMENT_FAILED,
            session=session,
            publisher=publisher,
        )

    with database.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.available_quantity == 10  # fully restored
        assert item.reserved_quantity == 0

    released = drain(database, publisher).only_event_on(Topic.INVENTORY_RELEASED)
    assert released.payload["released_units"] == 4


def test_payment_failure_for_an_order_that_never_reserved_is_a_no_op(database, publisher):
    """Reservation failed earlier, so there is nothing to compensate."""
    with database.session() as session:
        handle_payment_failed(
            EventEnvelope(
                event_type=EventType.PAYMENT_FAILED,
                source="payment-service",
                payload={"order_id": str(uuid.uuid4())},
            ),
            Topic.PAYMENT_FAILED,
            session=session,
            publisher=publisher,
        )

    released = drain(database, publisher).only_event_on(Topic.INVENTORY_RELEASED)
    assert released.payload["released_units"] == 0


def test_shipping_commits_the_reservation_permanently(database, publisher):
    location_id, product_id = _stock(database, 10)
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_order_created(
            order_created(order_id, product_id, 3, location_id),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )
    with database.session() as session:
        handle_order_shipped(
            EventEnvelope(
                event_type=EventType.ORDER_SHIPPED,
                source="fulfilment-service",
                payload={"order_id": str(order_id)},
            ),
            Topic.ORDER_SHIPPED,
            session=session,
            publisher=publisher,
        )

    with database.session() as session:
        item = InventoryService(session).get_item(product_id, location_id)
        assert item.reserved_quantity == 0
        assert item.available_quantity == 7
        assert item.total_quantity == 7  # the 3 units left the building


# ---------------------------------------------------------------------------
# Malformed events
# ---------------------------------------------------------------------------
def test_missing_order_id_fails_permanently(database, publisher):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_created(
            EventEnvelope(
                event_type=EventType.ORDER_CREATED, source="order-service", payload={"lines": []}
            ),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )


def test_missing_lines_fails_permanently(database, publisher):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_created(
            EventEnvelope(
                event_type=EventType.ORDER_CREATED,
                source="order-service",
                payload={"order_id": str(uuid.uuid4())},
            ),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )


def test_malformed_line_fails_permanently(database, publisher):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_created(
            EventEnvelope(
                event_type=EventType.ORDER_CREATED,
                source="order-service",
                payload={
                    "order_id": str(uuid.uuid4()),
                    "lines": [{"product_id": "not-a-uuid", "quantity": 1}],
                },
            ),
            Topic.ORDER_CREATED,
            session=session,
            publisher=publisher,
        )


def test_malformed_event_reaches_the_inventory_dlq_without_retrying(database, publisher):
    processor = EventProcessor(
        service_name="inventory-service",
        consumer_group=CONSUMER_GROUP,
        publisher=publisher,
        sleep=lambda _: None,
    )

    def dispatch(evt, topic):
        with database.session() as session:
            handle_order_created(evt, topic, session=session, publisher=publisher)

    assert processor.process(
        EventEnvelope(
            event_type=EventType.ORDER_CREATED, source="order-service", payload={}
        ),
        Topic.ORDER_CREATED,
        dispatch,
    )

    assert drain(database, publisher).topics() == [DeadLetterTopic.ORDERS]
