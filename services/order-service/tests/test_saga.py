"""Saga tests for the order service.

The full distributed flow is exercised without a broker by handing events
directly to the handlers. That keeps these fast enough to run on every push,
and the broker-level concerns (delivery, offsets) are covered separately by
the Kafka integration tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC

import pytest

from app.handlers import (
    CONSUMER_GROUP,
    handle_inventory_failed,
    handle_inventory_released,
    handle_inventory_reserved,
    handle_payment_confirmed,
    handle_payment_failed,
)
from retailpulse_common.events.consumer import EventProcessor, PermanentEventError, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import IdempotencyGuard
from retailpulse_common.events.outbox import OutboxEvent, OutboxRelay
from retailpulse_common.events.producer import InMemoryEventPublisher
from retailpulse_common.events.topics import DeadLetterTopic, EventType, Topic
from tests.conftest import SHIPPING_ADDRESS, WIDGET_ID


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


def _event(event_type: str, source: str, **payload) -> EventEnvelope:
    return EventEnvelope(event_type=event_type, source=source, payload=payload)


# ---------------------------------------------------------------------------
# The outbox
# ---------------------------------------------------------------------------
def test_creating_an_order_stages_the_event_in_the_outbox(client, customer_headers, database):
    """Not published directly -- staged atomically with the order row."""
    order = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 2}],
        },
        headers=customer_headers,
    ).json()

    with database.session() as session:
        rows = session.query(OutboxEvent).all()
        assert len(rows) == 1
        assert rows[0].topic == Topic.ORDER_CREATED
        assert rows[0].published_at is None
        # Keyed by order_id so every event for this order shares a partition.
        assert rows[0].partition_key == order["order_id"]


def test_outbox_payload_carries_everything_downstream_needs(client, customer_headers, database):
    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 3}],
        },
        headers=customer_headers,
    )

    with database.session() as session:
        event = EventEnvelope.from_json(session.query(OutboxEvent).one().body)

    assert event.event_type == EventType.ORDER_CREATED
    assert event.payload["lines"][0]["quantity"] == 3
    assert event.payload["lines"][0]["sku"] == "WIDGET-001"
    assert event.payload["total_amount"] == "599.97"
    assert event.payload["shipping_address"] == SHIPPING_ADDRESS


def test_a_rejected_order_stages_no_event(client, customer_headers, database):
    """The order rolled back, so its event must roll back with it."""
    response = client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
        },
        headers=customer_headers,
    )

    assert response.status_code == 400
    with database.session() as session:
        assert session.query(OutboxEvent).count() == 0


def test_relay_publishes_and_marks_the_row(client, customer_headers, database, publisher):
    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )

    published = OutboxRelay(database, publisher).publish_pending()

    assert published == 1
    assert drain(database, publisher).topics() == [Topic.ORDER_CREATED]
    with database.session() as session:
        assert session.query(OutboxEvent).one().published_at is not None


def test_relay_does_not_republish_already_published_rows(client, customer_headers, database, publisher):
    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )
    relay = OutboxRelay(database, publisher)
    relay.publish_pending()

    assert relay.publish_pending() == 0
    assert len(publisher.published) == 1


def test_a_failed_publish_leaves_the_row_for_the_next_pass(client, customer_headers, database):
    """The event is owed to the system; a broker outage must not lose it."""

    class BrokenPublisher:
        def publish(self, *a, **k):
            raise RuntimeError("broker down")

        def flush(self, timeout_seconds: float = 10.0) -> int:
            return 0

    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )

    assert OutboxRelay(database, BrokenPublisher()).publish_pending() == 0

    with database.session() as session:
        row = session.query(OutboxEvent).one()
        assert row.published_at is None  # still owed
        assert row.attempts == 1
        assert "broker down" in row.last_error

    # Recovers once the broker is back.
    good = InMemoryEventPublisher()
    assert OutboxRelay(database, good).publish_pending() == 1


def test_relay_gives_up_after_max_attempts_and_reports_stuck(client, customer_headers, database):
    class BrokenPublisher:
        def publish(self, *a, **k):
            raise RuntimeError("permanently broken")

        def flush(self, timeout_seconds: float = 10.0) -> int:
            return 0

    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )
    relay = OutboxRelay(database, BrokenPublisher(), max_attempts=3)

    for _ in range(5):
        relay.publish_pending()

    with database.session() as session:
        assert session.query(OutboxEvent).one().attempts == 3  # bounded
    assert len(relay.stuck_events()) == 1  # surfaced for a human


def test_prune_removes_published_rows_but_never_unpublished_ones(client, customer_headers, database, publisher):
    from datetime import datetime, timedelta

    client.post(
        "/orders",
        json={
            "shipping_address": SHIPPING_ADDRESS,
            "lines": [{"product_id": str(WIDGET_ID), "quantity": 1}],
        },
        headers=customer_headers,
    )
    relay = OutboxRelay(database, publisher)
    assert relay.publish_pending() == 1

    # An unpublished row, deliberately older than anything, that must survive
    # pruning regardless -- it is still owed to the rest of the platform.
    with database.session() as session:
        session.add(
            OutboxEvent(
                event_id=uuid.uuid4(),
                topic=Topic.ORDER_CANCELLED,
                partition_key="never-published",
                body=EventEnvelope(event_type="X", source="test").to_json(),
                created_at=datetime.now(UTC) - timedelta(days=365),
            )
        )

    # Nothing is older than the default window yet.
    assert relay.prune() == 0

    # Backdate the published row rather than pruning with a zero window: with
    # a zero window `cutoff` can land on the same microsecond as published_at
    # and the strict `<` would fail intermittently in CI.
    with database.session() as session:
        row = session.query(OutboxEvent).filter(OutboxEvent.published_at.is_not(None)).one()
        row.published_at = datetime.now(UTC) - timedelta(days=30)

    assert relay.prune() == 1

    with database.session() as session:
        remaining = session.query(OutboxEvent).all()
        assert len(remaining) == 1
        assert remaining[0].published_at is None  # the one still owed survived


# ---------------------------------------------------------------------------
# Reacting to the inventory outcome
# ---------------------------------------------------------------------------
def test_inventory_reserved_advances_the_order_and_requests_payment(
    client, customer_headers, database, publisher, placed_order
):
    order_id = placed_order["order_id"]
    event = _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=order_id)

    with database.session() as session:
        handle_inventory_reserved(
            event, Topic.INVENTORY_RESERVED, session=session, publisher=publisher
        )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    assert body["status"] == "INVENTORY_RESERVED"
    # The drain also flushes the ORDER_CREATED staged when the order was placed.
    assert drain(database, publisher).topics() == [
        Topic.ORDER_CREATED,
        Topic.PAYMENT_REQUESTED,
    ]
    payment = drain(database, publisher).only_event_on(Topic.PAYMENT_REQUESTED)
    assert payment.payload["amount"] == placed_order["total_amount"]
    # Same saga, so the correlation id is carried through.
    assert payment.correlation_id == event.correlation_id


def test_inventory_failed_cancels_the_order(
    client, customer_headers, database, publisher, placed_order
):
    order_id = placed_order["order_id"]
    event = _event(
        EventType.INVENTORY_FAILED,
        "inventory-service",
        order_id=order_id,
        reason="insufficient_inventory",
    )

    with database.session() as session:
        handle_inventory_failed(
            event, Topic.INVENTORY_FAILED, session=session, publisher=publisher
        )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    assert body["status"] == "CANCELLED"
    assert body["cancellation_reason"] == "insufficient_inventory"
    assert Topic.ORDER_CANCELLED in drain(database, publisher).topics()


def test_payment_confirmed_moves_through_to_confirmed(
    client, customer_headers, database, publisher, placed_order
):
    order_id = placed_order["order_id"]

    with database.session() as session:
        handle_inventory_reserved(
            _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=order_id),
            Topic.INVENTORY_RESERVED,
            session=session,
            publisher=publisher,
        )
    with database.session() as session:
        handle_payment_confirmed(
            _event(EventType.PAYMENT_CONFIRMED, "payment-service", order_id=order_id),
            Topic.PAYMENT_CONFIRMED,
            session=session,
            publisher=publisher,
        )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    # PAYMENT_CONFIRMED and then straight on to CONFIRMED.
    assert body["status"] == "CONFIRMED"
    assert Topic.ORDER_CONFIRMED in drain(database, publisher).topics()


def test_full_payment_failure_compensation_saga(
    client, customer_headers, database, publisher, placed_order
):
    """reserved -> payment failed -> stock released -> cancelled."""
    order_id = placed_order["order_id"]

    steps = [
        (handle_inventory_reserved, EventType.INVENTORY_RESERVED, Topic.INVENTORY_RESERVED, {}),
        (handle_payment_failed, EventType.PAYMENT_FAILED, Topic.PAYMENT_FAILED, {"reason": "CARD_DECLINED"}),
        (handle_inventory_released, EventType.INVENTORY_RELEASED, Topic.INVENTORY_RELEASED, {}),
    ]
    for handler, event_type, topic, extra in steps:
        with database.session() as session:
            handler(
                _event(event_type, "svc", order_id=order_id, **extra),
                topic,
                session=session,
                publisher=publisher,
            )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    assert body["status"] == "CANCELLED"
    statuses = [t["to_status"] for t in body["transitions"]]
    assert statuses == [
        "CREATED",
        "INVENTORY_RESERVED",
        "PAYMENT_FAILED",
        "INVENTORY_RELEASED",
        "CANCELLED",
    ]


# ---------------------------------------------------------------------------
# Idempotency of the saga
# ---------------------------------------------------------------------------
def test_redelivered_event_is_rejected_by_the_idempotency_guard(
    client, database, publisher, placed_order
):
    """The same event twice must not advance the order twice."""
    from retailpulse_common.events.idempotency import DuplicateEventError

    order_id = placed_order["order_id"]
    event = _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=order_id)

    with database.session() as session:
        handle_inventory_reserved(
            event, Topic.INVENTORY_RESERVED, session=session, publisher=publisher
        )

    with pytest.raises(DuplicateEventError), database.session() as session:
        handle_inventory_reserved(
            event, Topic.INVENTORY_RESERVED, session=session, publisher=publisher
        )

    # Exactly one payment request, not two.
    assert drain(database, publisher).topics().count(Topic.PAYMENT_REQUESTED) == 1


def test_processor_treats_a_redelivered_event_as_success(
    database, publisher, placed_order
):
    """End to end: the consumer commits the offset rather than dead-lettering."""
    order_id = placed_order["order_id"]
    event = _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=order_id)

    def dispatch(evt, topic):
        with database.session() as session:
            handle_inventory_reserved(evt, topic, session=session, publisher=publisher)

    processor = EventProcessor(
        service_name="order-service",
        consumer_group=CONSUMER_GROUP,
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=lambda _: None,
    )

    assert processor.process(event, Topic.INVENTORY_RESERVED, dispatch)
    assert processor.process(event, Topic.INVENTORY_RESERVED, dispatch)

    # Committed both times, dead-lettered neither, acted once.
    assert drain(database, publisher).topics().count(Topic.PAYMENT_REQUESTED) == 1
    assert DeadLetterTopic.ORDERS not in drain(database, publisher).topics()


def test_processed_events_records_the_consumer_group(database, publisher, placed_order):
    order_id = placed_order["order_id"]
    event = _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=order_id)

    with database.session() as session:
        handle_inventory_reserved(
            event, Topic.INVENTORY_RESERVED, session=session, publisher=publisher
        )

    with database.session() as session:
        assert IdempotencyGuard(session, CONSUMER_GROUP).has_processed(event.event_id)
        # A different consumer group has not seen it.
        assert not IdempotencyGuard(session, "analytics-service").has_processed(event.event_id)


# ---------------------------------------------------------------------------
# Malformed and out-of-order events
# ---------------------------------------------------------------------------
def test_event_without_order_id_fails_permanently(database, publisher):
    """No retries: the field will still be missing next time."""
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_inventory_reserved(
            _event(EventType.INVENTORY_RESERVED, "inventory-service"),
            Topic.INVENTORY_RESERVED,
            session=session,
            publisher=publisher,
        )


def test_event_with_a_non_uuid_order_id_fails_permanently(database, publisher):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_inventory_reserved(
            _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id="not-a-uuid"),
            Topic.INVENTORY_RESERVED,
            session=session,
            publisher=publisher,
        )


def test_event_for_an_unknown_order_fails_permanently(database, publisher):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_inventory_reserved(
            _event(EventType.INVENTORY_RESERVED, "inventory-service", order_id=str(uuid.uuid4())),
            Topic.INVENTORY_RESERVED,
            session=session,
            publisher=publisher,
        )


def test_malformed_event_is_dead_lettered_without_retrying(database, publisher):
    processor = EventProcessor(
        service_name="order-service",
        consumer_group=CONSUMER_GROUP,
        publisher=publisher,
        sleep=lambda _: None,
    )

    def dispatch(evt, topic):
        with database.session() as session:
            handle_inventory_reserved(evt, topic, session=session, publisher=publisher)

    assert processor.process(
        _event(EventType.INVENTORY_RESERVED, "inventory-service"),
        Topic.INVENTORY_RESERVED,
        dispatch,
    )
    # Routed by the topic it arrived on, not by which service consumed it.
    assert DeadLetterTopic.INVENTORY in drain(database, publisher).topics()


def test_out_of_order_event_is_ignored_not_dead_lettered(
    client, customer_headers, database, publisher, placed_order
):
    """A stale event that would make an illegal transition is harmless.

    The state machine already refused it, so dead-lettering would just create
    noise for an operator to triage.
    """
    order_id = placed_order["order_id"]

    # PAYMENT_CONFIRMED arriving while the order is still CREATED.
    with database.session() as session:
        handle_payment_confirmed(
            _event(EventType.PAYMENT_CONFIRMED, "payment-service", order_id=order_id),
            Topic.PAYMENT_CONFIRMED,
            session=session,
            publisher=publisher,
        )

    body = client.get(f"/orders/{order_id}", headers=customer_headers).json()
    assert body["status"] == "CREATED"  # unchanged
    assert DeadLetterTopic.ORDERS not in drain(database, publisher).topics()
