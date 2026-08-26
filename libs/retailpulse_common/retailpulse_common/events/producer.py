"""Publishing events to Kafka.

Producer configuration is where durability is won or lost, so the choices are
explicit rather than left to defaults:

``acks=all``
    Wait until every in-sync replica has the record. The default (``acks=1``)
    acknowledges as soon as the leader has it, so a leader failure between ack
    and replication silently loses the event. For an ORDER_CREATED that means a
    paid-for order that no downstream service ever hears about.

``enable.idempotence=true``
    Without it, a producer-side retry after a network blip can write the same
    record twice. With it, the broker de-duplicates by sequence number, so a
    retry is safe. This also pins ``acks=all`` and bounds in-flight requests.

``compression.type=snappy``
    Event bodies are JSON, which compresses well. Cheaper network and disk for
    a small CPU cost.

``linger.ms``
    A few milliseconds of batching. Set to 0 the producer sends one request per
    record, which wastes throughput; set too high it adds latency to the
    checkout path.
"""

from __future__ import annotations

import atexit
import logging
import uuid
from typing import Any, Protocol

from retailpulse_common.errors import ServiceUnavailableError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.observability import KAFKA_EVENTS_PRODUCED_TOTAL

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    """What the services depend on. Implemented by Kafka and by a test double."""

    def publish(self, topic: str, event: EventEnvelope, *, key: str | None = None) -> None:
        ...

    def flush(self, timeout_seconds: float = 10.0) -> int:
        ...


class KafkaEventPublisher:
    """Thin wrapper over confluent_kafka.Producer."""

    def __init__(
        self,
        bootstrap_servers: str,
        source: str,
        *,
        linger_ms: int = 5,
        request_timeout_ms: int = 10_000,
    ) -> None:
        from confluent_kafka import Producer

        self.source = source
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "acks": "all",
                "enable.idempotence": True,
                "compression.type": "snappy",
                "linger.ms": linger_ms,
                "request.timeout.ms": request_timeout_ms,
                # Bounded retries. Infinite retries would hold a request thread
                # forever when the broker is genuinely gone.
                "retries": 5,
                "retry.backoff.ms": 200,
                "client.id": source,
            }
        )
        # Flush on interpreter exit so a clean shutdown does not drop events
        # still sitting in the producer's internal queue.
        atexit.register(self._safe_flush)

    def _delivery_report(self, err: Any, msg: Any) -> None:
        if err is not None:
            # The event is lost. Loud, because a silent drop here is a business
            # transaction that never happened downstream.
            logger.error(
                "event delivery failed",
                extra={"topic": msg.topic() if msg else None, "error": str(err)},
            )
            return
        logger.debug(
            "event delivered",
            extra={
                "topic": msg.topic(),
                "partition": msg.partition(),
                "offset": msg.offset(),
            },
        )

    def publish(self, topic: str, event: EventEnvelope, *, key: str | None = None) -> None:
        """Queue an event for delivery.

        Asynchronous by design: blocking the HTTP request until the broker
        acknowledges would tie checkout latency to Kafka's. Delivery outcome is
        reported through the callback.
        """
        from confluent_kafka import KafkaException

        try:
            self._producer.produce(
                topic=topic,
                key=(key or str(event.event_id)).encode("utf-8"),
                value=event.to_json().encode("utf-8"),
                on_delivery=self._delivery_report,
            )
            # Serve delivery callbacks without blocking.
            self._producer.poll(0)
        except BufferError as exc:
            # The local queue is full, meaning the broker is not keeping up.
            # Surfacing 503 is honest; silently dropping would not be.
            logger.error("producer queue full", extra={"topic": topic})
            raise ServiceUnavailableError(
                "The event bus is not accepting events right now. Please retry."
            ) from exc
        except KafkaException as exc:
            logger.error("produce failed", extra={"topic": topic, "error": str(exc)})
            raise ServiceUnavailableError("Could not publish to the event bus.") from exc

        KAFKA_EVENTS_PRODUCED_TOTAL.labels(self.source, topic).inc()
        logger.info(
            "event published",
            extra={
                "topic": topic,
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "correlation_id": str(event.correlation_id),
            },
        )

    def flush(self, timeout_seconds: float = 10.0) -> int:
        """Block until queued events are delivered. Returns the number still pending."""
        return self._producer.flush(timeout_seconds)

    def _safe_flush(self) -> None:  # pragma: no cover - shutdown path
        try:
            remaining = self._producer.flush(5.0)
        except Exception:
            # Interpreter shutdown: never raise from atexit, but say so.
            logger.warning("producer flush failed during shutdown", exc_info=True)
            return
        if remaining:
            logger.error(
                "events dropped at shutdown; they were never delivered",
                extra={"undelivered": remaining},
            )


class InMemoryEventPublisher:
    """Test double that records what would have been published.

    Lets the whole saga be tested without a broker, which keeps the unit suite
    fast and runnable in CI with no services attached.
    """

    def __init__(self, source: str = "test") -> None:
        self.source = source
        self.published: list[tuple[str, EventEnvelope, str | None]] = []

    def publish(self, topic: str, event: EventEnvelope, *, key: str | None = None) -> None:
        self.published.append((topic, event, key))
        KAFKA_EVENTS_PRODUCED_TOTAL.labels(self.source, topic).inc()

    def flush(self, timeout_seconds: float = 10.0) -> int:  # noqa: ARG002
        """Nothing is buffered, but the signature must match the protocol."""
        return 0

    # -- assertions used by tests -------------------------------------
    def events_on(self, topic: str) -> list[EventEnvelope]:
        return [event for published_topic, event, _ in self.published if published_topic == topic]

    def topics(self) -> list[str]:
        return [topic for topic, _, _ in self.published]

    def only_event_on(self, topic: str) -> EventEnvelope:
        events = self.events_on(topic)
        if len(events) != 1:
            raise AssertionError(
                f"expected exactly one event on {topic!r}, found {len(events)}"
            )
        return events[0]

    def keys_on(self, topic: str) -> list[str | None]:
        return [key for published_topic, _, key in self.published if published_topic == topic]

    def clear(self) -> None:
        self.published.clear()


def order_key(order_id: uuid.UUID | str) -> str:
    """Partition key for order-scoped events.

    Kafka preserves ordering only within a partition, and equal keys always
    land on the same partition. Keying every event in an order's lifecycle by
    ``order_id`` is what stops INVENTORY_RESERVED from being processed before
    the ORDER_CREATED that caused it.
    """
    return str(order_id)
