"""Tests for the event envelope, retry policy, DLQ routing and idempotency.

These paths only execute when something has already gone wrong, which is
exactly why they get the most thorough tests in the codebase -- a bug here is
invisible until an incident is already in progress.
"""

from __future__ import annotations

import uuid

import pytest

from retailpulse_common.events.consumer import (
    EventProcessor,
    PermanentEventError,
    RetryPolicy,
)
from retailpulse_common.events.envelope import DeadLetterEnvelope, EventEnvelope
from retailpulse_common.events.idempotency import (
    DuplicateEventError,
    IdempotencyGuard,
    ProcessedEvent,
)
from retailpulse_common.events.producer import InMemoryEventPublisher, order_key
from retailpulse_common.events.topics import (
    ALL_DLQ_TOPICS,
    ALL_TOPICS,
    DeadLetterTopic,
    EventType,
    Topic,
    dlq_for,
)


@pytest.fixture()
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher(source="test-service")


@pytest.fixture()
def sleeps() -> list[float]:
    return []


@pytest.fixture()
def processor(publisher, sleeps) -> EventProcessor:
    return EventProcessor(
        service_name="test-service",
        consumer_group="test-group",
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.5),
        sleep=sleeps.append,  # record delays instead of actually sleeping
    )


def make_event(event_type: str = EventType.ORDER_CREATED, **payload) -> EventEnvelope:
    return EventEnvelope(
        event_type=event_type, source="order-service", payload=payload or {"order_id": "x"}
    )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
def test_envelope_round_trips_through_json():
    original = make_event(order_id="abc", total="199.99")

    restored = EventEnvelope.from_json(original.to_json())

    assert restored.event_id == original.event_id
    assert restored.event_type == original.event_type
    assert restored.payload == original.payload
    assert restored.correlation_id == original.correlation_id


def test_each_event_gets_a_unique_id():
    assert make_event().event_id != make_event().event_id


def test_envelope_rejects_unknown_fields():
    """extra='forbid' catches a producer sending a field consumers ignore."""
    with pytest.raises(Exception):
        EventEnvelope.model_validate(
            {"event_type": "X", "source": "s", "unexpected_field": 1}
        )


def test_child_event_keeps_the_correlation_id_but_gets_a_new_event_id():
    """This is what makes a whole saga traceable from one filter."""
    parent = make_event()

    child = parent.child(
        event_type=EventType.INVENTORY_RESERVED,
        source="inventory-service",
        payload={"order_id": "abc"},
    )

    assert child.correlation_id == parent.correlation_id
    assert child.event_id != parent.event_id
    assert child.source == "inventory-service"


def test_timestamp_is_timezone_aware():
    """Naive timestamps are ambiguous the moment two services differ in TZ."""
    assert make_event().timestamp.tzinfo is not None


def test_version_defaults_to_one():
    assert make_event().version == 1


# ---------------------------------------------------------------------------
# Topic registry
# ---------------------------------------------------------------------------
def test_every_topic_routes_to_a_dead_letter_topic():
    for topic in ALL_TOPICS:
        assert dlq_for(topic) in ALL_DLQ_TOPICS


def test_order_topics_route_to_the_orders_dlq():
    assert dlq_for(Topic.ORDER_CREATED) == DeadLetterTopic.ORDERS


def test_inventory_topics_route_to_the_inventory_dlq():
    assert dlq_for(Topic.INVENTORY_RESERVED) == DeadLetterTopic.INVENTORY
    assert dlq_for(Topic.INVENTORY_FAILED) == DeadLetterTopic.INVENTORY


def test_payment_topics_route_to_the_payments_dlq():
    assert dlq_for(Topic.PAYMENT_FAILED) == DeadLetterTopic.PAYMENTS


def test_unknown_topic_still_gets_a_dlq_rather_than_raising():
    """Losing a message to a typo'd mapping is worse than a wrong-but-present DLQ."""
    assert dlq_for("some.topic.that.does.not.exist") == DeadLetterTopic.ORDERS


def test_topic_names_are_unique():
    assert len(set(ALL_TOPICS)) == len(ALL_TOPICS)


def test_order_key_is_stable_for_the_same_order():
    """Equal keys hash to the same partition, which is what preserves ordering."""
    order_id = uuid.uuid4()
    assert order_key(order_id) == order_key(str(order_id))


# ---------------------------------------------------------------------------
# Successful processing
# ---------------------------------------------------------------------------
def test_successful_handler_commits_and_publishes_nothing_to_the_dlq(processor, publisher):
    handled = []

    assert processor.process(make_event(), Topic.ORDER_CREATED, lambda e, t: handled.append(e))
    assert len(handled) == 1
    assert publisher.published == []


def test_handler_receives_the_topic_it_arrived_on(processor):
    seen = {}
    processor.process(
        make_event(), Topic.ORDER_CREATED, lambda e, t: seen.update({"topic": t})
    )
    assert seen["topic"] == Topic.ORDER_CREATED


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
def test_transient_failure_is_retried_then_succeeds(processor, publisher, sleeps):
    attempts = []

    def flaky(event, topic):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("database briefly unavailable")

    assert processor.process(make_event(), Topic.ORDER_CREATED, flaky)

    assert len(attempts) == 3
    assert publisher.published == []  # recovered, so never dead-lettered
    assert len(sleeps) == 2  # backoff between the three attempts


def test_retries_are_bounded_and_then_dead_lettered(processor, publisher):
    attempts = []

    def always_fails(event, topic):
        attempts.append(1)
        raise ConnectionError("still down")

    assert processor.process(make_event(), Topic.ORDER_CREATED, always_fails)

    assert len(attempts) == 3, "must not retry forever"
    assert publisher.topics() == [DeadLetterTopic.ORDERS]


def test_backoff_grows_exponentially():
    policy = RetryPolicy(max_attempts=5, backoff_seconds=0.5, backoff_multiplier=2.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]


def test_backoff_is_capped():
    """Uncapped growth would hold a partition for minutes."""
    policy = RetryPolicy(
        max_attempts=20, backoff_seconds=1.0, backoff_multiplier=2.0, max_backoff_seconds=10.0
    )
    assert policy.delay_for(15) == 10.0


def test_a_single_attempt_policy_dead_letters_immediately(publisher):
    processor = EventProcessor(
        service_name="s",
        consumer_group="g",
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=1),
        sleep=lambda _: None,
    )

    processor.process(make_event(), Topic.ORDER_CREATED, _raise(RuntimeError("nope")))

    assert publisher.topics() == [DeadLetterTopic.ORDERS]


# ---------------------------------------------------------------------------
# Permanent failures skip retries
# ---------------------------------------------------------------------------
def test_permanent_failure_is_not_retried(processor, publisher, sleeps):
    """Retrying a malformed payload just blocks the partition."""
    attempts = []

    def malformed(event, topic):
        attempts.append(1)
        raise PermanentEventError("payload is missing order_id")

    assert processor.process(make_event(), Topic.ORDER_CREATED, malformed)

    assert len(attempts) == 1
    assert sleeps == []  # no backoff at all
    assert publisher.topics() == [DeadLetterTopic.ORDERS]


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------
def test_duplicate_event_is_acknowledged_not_dead_lettered(processor, publisher):
    """A redelivered event is normal operation, not a failure."""
    event = make_event()

    def duplicate(e, t):
        raise DuplicateEventError(e.event_id, "test-group")

    assert processor.process(event, Topic.ORDER_CREATED, duplicate)
    assert publisher.published == []


# ---------------------------------------------------------------------------
# Dead letter contents
# ---------------------------------------------------------------------------
def test_dead_letter_preserves_the_original_event_and_failure_context(processor, publisher):
    event = make_event(order_id="order-123")

    processor.process(event, Topic.ORDER_CREATED, _raise(ValueError("bad thing")))

    dlq_event = publisher.only_event_on(DeadLetterTopic.ORDERS)
    letter = DeadLetterEnvelope.model_validate(dlq_event.payload)

    assert letter.original_topic == Topic.ORDER_CREATED
    assert letter.consumer_group == "test-group"
    assert letter.attempts == 3
    assert letter.error_type == "ValueError"
    assert "bad thing" in letter.error_message
    # Replayable: the original event survives verbatim.
    assert letter.original_event.event_id == event.event_id
    assert letter.original_event.payload == {"order_id": "order-123"}


def test_dead_letter_keeps_the_correlation_id(processor, publisher):
    event = make_event()
    processor.process(event, Topic.ORDER_CREATED, _raise(RuntimeError("x")))
    assert publisher.only_event_on(DeadLetterTopic.ORDERS).correlation_id == event.correlation_id


def test_dead_letter_is_keyed_by_event_id(processor, publisher):
    event = make_event()
    processor.process(event, Topic.ORDER_CREATED, _raise(RuntimeError("x")))
    assert publisher.keys_on(DeadLetterTopic.ORDERS) == [str(event.event_id)]


def test_inventory_failure_lands_in_the_inventory_dlq(processor, publisher):
    processor.process(
        make_event(EventType.INVENTORY_RESERVED),
        Topic.INVENTORY_RESERVED,
        _raise(RuntimeError("x")),
    )
    assert publisher.topics() == [DeadLetterTopic.INVENTORY]


def test_offset_is_still_committed_when_the_dlq_write_fails(processor):
    """Nowhere left to put it, but the pipeline must not wedge."""

    class BrokenPublisher:
        def publish(self, *a, **k):
            raise RuntimeError("kafka is gone")

        def flush(self, timeout_seconds: float = 10.0) -> int:
            return 0

    processor.publisher = BrokenPublisher()

    assert processor.process(make_event(), Topic.ORDER_CREATED, _raise(ValueError("x")))


def _raise(exc: Exception):
    def handler(event, topic):
        raise exc

    return handler


# ---------------------------------------------------------------------------
# Idempotency guard (against a real database)
# ---------------------------------------------------------------------------
@pytest.fixture()
def db():
    from retailpulse_common.db import Database

    database = Database("sqlite+pysqlite:///:memory:")
    ProcessedEvent.__table__.create(database.engine)
    return database


def test_first_claim_succeeds(db):
    event_id = uuid.uuid4()
    with db.session() as session:
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )

    with db.session() as session:
        assert IdempotencyGuard(session, "orders").has_processed(event_id)


def test_second_claim_of_the_same_event_is_rejected(db):
    event_id = uuid.uuid4()
    with db.session() as session:
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )

    with db.session() as session, pytest.raises(DuplicateEventError):
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )


def test_different_consumer_groups_both_process_the_same_event(db):
    """The order service and analytics both care about ORDER_CREATED."""
    event_id = uuid.uuid4()

    with db.session() as session:
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )
    with db.session() as session:
        IdempotencyGuard(session, "analytics").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )

    with db.session() as session:
        assert IdempotencyGuard(session, "orders").has_processed(event_id)
        assert IdempotencyGuard(session, "analytics").has_processed(event_id)


def test_a_rejected_claim_leaves_the_transaction_usable(db):
    """The savepoint means a duplicate does not poison the outer transaction."""
    event_id = uuid.uuid4()
    with db.session() as session:
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )

    with db.session() as session:
        guard = IdempotencyGuard(session, "orders")
        with pytest.raises(DuplicateEventError):
            guard.claim(
                event_id=event_id,
                event_type=EventType.ORDER_CREATED,
                topic=Topic.ORDER_CREATED,
            )
        # Still usable afterwards.
        assert guard.has_processed(event_id)
        other = uuid.uuid4()
        guard.claim(
            event_id=other, event_type=EventType.ORDER_CREATED, topic=Topic.ORDER_CREATED
        )


def test_claim_rolls_back_with_the_handler_when_side_effects_fail(db):
    """The claim and the work must commit together, or not at all."""
    event_id = uuid.uuid4()

    with pytest.raises(RuntimeError), db.session() as session:
        IdempotencyGuard(session, "orders").claim(
            event_id=event_id,
            event_type=EventType.ORDER_CREATED,
            topic=Topic.ORDER_CREATED,
        )
        raise RuntimeError("side effect failed")

    # The event must look unprocessed, so redelivery retries it.
    with db.session() as session:
        assert not IdempotencyGuard(session, "orders").has_processed(event_id)


def test_unprocessed_event_reports_false(db):
    with db.session() as session:
        assert not IdempotencyGuard(session, "orders").has_processed(uuid.uuid4())
