"""Event-driven plumbing shared by every service.

Import the pieces you need:

* :mod:`~retailpulse_common.events.envelope` -- the one message shape.
* :mod:`~retailpulse_common.events.topics` -- topic and event-type registry.
* :mod:`~retailpulse_common.events.producer` -- publishing.
* :mod:`~retailpulse_common.events.consumer` -- consuming, retries, DLQ.
* :mod:`~retailpulse_common.events.idempotency` -- duplicate suppression.

``idempotency`` is imported separately on purpose: it registers the
``processed_events`` table on the shared declarative base, and only services
that actually consume events should own that table.
"""

from retailpulse_common.events.envelope import DeadLetterEnvelope, EventEnvelope
from retailpulse_common.events.producer import (
    EventPublisher,
    InMemoryEventPublisher,
    KafkaEventPublisher,
    order_key,
)
from retailpulse_common.events.topics import (
    ALL_DLQ_TOPICS,
    ALL_TOPICS,
    DeadLetterTopic,
    EventType,
    Topic,
    dlq_for,
)

__all__ = [
    "ALL_DLQ_TOPICS",
    "ALL_TOPICS",
    "DeadLetterEnvelope",
    "DeadLetterTopic",
    "EventEnvelope",
    "EventPublisher",
    "EventType",
    "InMemoryEventPublisher",
    "KafkaEventPublisher",
    "Topic",
    "dlq_for",
    "order_key",
]
