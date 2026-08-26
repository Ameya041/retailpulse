"""Integration tests against a real Kafka broker.

The unit tests use an in-memory publisher, which proves the *logic* but not
that the wire format, partitioning and delivery guarantees actually work. These
tests publish to and consume from a live broker.

Skipped automatically when no broker is reachable, so `pytest` still passes on
a laptop with nothing running.

    docker compose up -d kafka
    pytest -m kafka
"""

from __future__ import annotations

import os
import uuid

import pytest

from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.producer import KafkaEventPublisher, order_key
from retailpulse_common.events.topics import EventType, Topic

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")


def _broker_available() -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        metadata = AdminClient({"bootstrap.servers": BOOTSTRAP}).list_topics(timeout=5)
        return bool(metadata.brokers)
    except Exception:
        return False


pytestmark = [
    pytest.mark.kafka,
    pytest.mark.skipif(
        not _broker_available(),
        reason="Kafka is not reachable; run `docker compose up -d kafka`",
    ),
]


@pytest.fixture(scope="module")
def publisher() -> KafkaEventPublisher:
    return KafkaEventPublisher(BOOTSTRAP, source="integration-test")


def _consume(topic: str, group_id: str, expected: int, timeout: float = 30.0):
    """Read up to `expected` messages from the beginning of a topic."""
    import time

    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([topic])

    collected = []
    deadline = time.time() + timeout
    try:
        while len(collected) < expected and time.time() < deadline:
            message = consumer.poll(1.0)
            if message is None or message.error():
                continue
            collected.append(message)
    finally:
        consumer.close()
    return collected


def test_event_round_trips_through_a_real_broker(publisher):
    """Publish, then consume it back and confirm nothing was lost in transit."""
    marker = uuid.uuid4()
    event = EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        source="integration-test",
        payload={"order_id": str(marker), "total_amount": "199.99"},
    )

    publisher.publish(Topic.ORDER_CREATED, event, key=order_key(marker))
    assert publisher.flush(15.0) == 0, "producer did not drain its queue"

    messages = _consume(Topic.ORDER_CREATED, f"itest-{uuid.uuid4()}", expected=50, timeout=20)
    received = [EventEnvelope.from_json(m.value()) for m in messages]
    match = [e for e in received if e.payload.get("order_id") == str(marker)]

    assert match, "published event was never received"
    assert match[0].event_id == event.event_id
    assert match[0].correlation_id == event.correlation_id
    assert match[0].payload["total_amount"] == "199.99"
    # Decimal-as-string survived: no float coercion anywhere on the wire.
    assert isinstance(match[0].payload["total_amount"], str)


def test_same_key_always_lands_on_the_same_partition(publisher):
    """This is what guarantees per-order event ordering."""
    order_id = uuid.uuid4()

    for i in range(5):
        publisher.publish(
            Topic.ORDER_CREATED,
            EventEnvelope(
                event_type=EventType.ORDER_CREATED,
                source="integration-test",
                payload={"order_id": str(order_id), "seq": i},
            ),
            key=order_key(order_id),
        )
    publisher.flush(15.0)

    messages = _consume(Topic.ORDER_CREATED, f"itest-{uuid.uuid4()}", expected=200, timeout=20)
    ours = [
        m
        for m in messages
        if EventEnvelope.from_json(m.value()).payload.get("order_id") == str(order_id)
    ]

    assert len(ours) >= 5
    assert len({m.partition() for m in ours}) == 1, "same key split across partitions"

    # And within that partition, offsets preserve publish order.
    seqs = [EventEnvelope.from_json(m.value()).payload["seq"] for m in sorted(ours, key=lambda m: m.offset())]
    assert seqs == sorted(seqs)


def test_different_keys_spread_across_partitions(publisher):
    """Otherwise the topic's partitions buy no parallelism at all."""
    published = [uuid.uuid4() for _ in range(40)]
    for order_id in published:
        publisher.publish(
            Topic.INVENTORY_RESERVED,
            EventEnvelope(
                event_type=EventType.INVENTORY_RESERVED,
                source="integration-test",
                payload={"order_id": str(order_id)},
            ),
            key=order_key(order_id),
        )
    publisher.flush(15.0)

    messages = _consume(
        Topic.INVENTORY_RESERVED, f"itest-{uuid.uuid4()}", expected=500, timeout=20
    )
    wanted = {str(o) for o in published}
    ours = [
        m
        for m in messages
        if EventEnvelope.from_json(m.value()).payload.get("order_id") in wanted
    ]

    assert len(ours) >= 40
    assert len({m.partition() for m in ours}) > 1


def test_dead_letter_topic_accepts_and_returns_a_wrapped_event(publisher):
    """A DLQ entry must be replayable: the original event survives verbatim."""
    from retailpulse_common.events.consumer import EventProcessor, RetryPolicy
    from retailpulse_common.events.envelope import DeadLetterEnvelope
    from retailpulse_common.events.topics import DeadLetterTopic

    marker = uuid.uuid4()
    original = EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        source="integration-test",
        payload={"order_id": str(marker)},
    )

    processor = EventProcessor(
        service_name="integration-test",
        consumer_group="itest-group",
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=1),
        sleep=lambda _: None,
    )

    def always_fails(event, topic):
        raise RuntimeError("handler exploded")

    assert processor.process(original, Topic.ORDER_CREATED, always_fails)
    publisher.flush(15.0)

    messages = _consume(
        DeadLetterTopic.ORDERS, f"itest-{uuid.uuid4()}", expected=100, timeout=20
    )
    letters = [
        DeadLetterEnvelope.model_validate(EventEnvelope.from_json(m.value()).payload)
        for m in messages
    ]
    ours = [
        letter
        for letter in letters
        if letter.original_event.payload.get("order_id") == str(marker)
    ]

    assert ours, "dead-lettered event never reached the DLQ topic"
    assert ours[0].original_event.event_id == original.event_id
    assert ours[0].error_type == "RuntimeError"
    assert "handler exploded" in ours[0].error_message
    assert ours[0].original_topic == Topic.ORDER_CREATED


def test_all_declared_topics_exist_on_the_broker():
    """Guards against publishing into a topic nobody created."""
    from confluent_kafka.admin import AdminClient

    from retailpulse_common.events.topics import ALL_DLQ_TOPICS, ALL_TOPICS

    existing = set(AdminClient({"bootstrap.servers": BOOTSTRAP}).list_topics(timeout=10).topics)

    missing = [t for t in (*ALL_TOPICS, *ALL_DLQ_TOPICS) if t not in existing]
    assert not missing, f"topics missing from broker: {missing} (run scripts/create_topics.py)"
