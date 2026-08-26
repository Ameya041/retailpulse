"""Consuming events, with bounded retries and a dead-letter topic.

## Offset handling

``enable.auto.commit`` is **off**. With auto-commit the client commits offsets
on a timer, which can acknowledge an event before the handler has finished --
so a crash loses it entirely. Committing manually, only after the handler
succeeds (or the event is dead-lettered), converts the failure mode from
"silently lost" into "processed twice", and duplicates are already handled by
the idempotency guard.

## Retries

Failures come in two flavours and must not be treated alike:

* **Transient** -- the database is briefly unreachable, a downstream service is
  restarting. Retrying works. These get a bounded number of attempts with
  exponential backoff.
* **Permanent** -- the payload is malformed, or references an entity that will
  never exist. Retrying is pure waste and, worse, blocks the partition behind a
  message that can never succeed. These go straight to the DLQ.

Retries are **in-process and bounded**. Infinite retries are how one poison
message halts an entire partition indefinitely.

## Dead letter topic

After the attempt budget is exhausted the original event is wrapped with its
failure context and published to the domain's DLQ, the offset is committed, and
the consumer moves on. The pipeline keeps flowing; nothing is lost; a human can
inspect and replay.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any

from retailpulse_common.events.envelope import DeadLetterEnvelope, EventEnvelope
from retailpulse_common.events.idempotency import DuplicateEventError
from retailpulse_common.events.producer import EventPublisher
from retailpulse_common.events.topics import dlq_for
from retailpulse_common.observability import (
    KAFKA_EVENTS_DUPLICATE_TOTAL,
    KAFKA_EVENTS_FAILED_TOTAL,
    KAFKA_EVENTS_PROCESSED_TOTAL,
)

logger = logging.getLogger(__name__)

#: A handler receives the parsed envelope and the topic it arrived on.
EventHandler = Callable[[EventEnvelope, str], None]


class PermanentEventError(Exception):
    """The event can never succeed. Skip retries and dead-letter immediately.

    Raise this for malformed payloads or references to entities that will
    never exist. Retrying such an event just burns the budget and delays every
    message behind it on the same partition.
    """


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 10.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff, capped.

        Uncapped exponential growth means attempt 10 sleeps for eight minutes,
        holding the partition the whole time.
        """
        delay = self.backoff_seconds * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_backoff_seconds)


class EventProcessor:
    """Broker-agnostic retry/DLQ logic.

    Kept separate from the Kafka client so the retry and dead-letter behaviour
    can be tested exhaustively without a broker -- those paths are the ones
    that only ever run when something is already going wrong, so they need to
    be the best-tested code in the system.
    """

    def __init__(
        self,
        *,
        service_name: str,
        consumer_group: str,
        publisher: EventPublisher,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.service_name = service_name
        self.consumer_group = consumer_group
        self.publisher = publisher
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def process(self, event: EventEnvelope, topic: str, handler: EventHandler) -> bool:
        """Run a handler with retries. Returns True if the offset may be committed.

        The return value is True even when the event was dead-lettered: the
        message has been dealt with, and not committing would replay it forever.
        """
        attempt = 0
        last_error: Exception | None = None

        while attempt < self.retry_policy.max_attempts:
            attempt += 1
            try:
                handler(event, topic)
            except DuplicateEventError:
                # Already handled by this consumer group. Success, not failure.
                KAFKA_EVENTS_DUPLICATE_TOTAL.labels(self.service_name, topic).inc()
                logger.info(
                    "duplicate event acknowledged",
                    extra={"event_id": str(event.event_id), "topic": topic},
                )
                return True
            except PermanentEventError as exc:
                logger.error(
                    "permanent failure; dead-lettering without retry",
                    extra={
                        "event_id": str(event.event_id),
                        "topic": topic,
                        "error": str(exc),
                    },
                )
                self._dead_letter(event, topic, attempt, exc)
                return True
            except Exception as exc:  # noqa: BLE001 - deliberate catch-all
                last_error = exc
                logger.warning(
                    "event handler failed",
                    extra={
                        "event_id": str(event.event_id),
                        "topic": topic,
                        "attempt": attempt,
                        "max_attempts": self.retry_policy.max_attempts,
                        "error": str(exc),
                    },
                )
                if attempt < self.retry_policy.max_attempts:
                    self._sleep(self.retry_policy.delay_for(attempt))
                continue
            else:
                KAFKA_EVENTS_PROCESSED_TOTAL.labels(self.service_name, topic).inc()
                logger.info(
                    "event processed",
                    extra={
                        "event_id": str(event.event_id),
                        "event_type": event.event_type,
                        "topic": topic,
                        "attempts": attempt,
                        "correlation_id": str(event.correlation_id),
                    },
                )
                return True

        # Attempt budget exhausted. `last_error` is always set here (the loop
        # only exits this way after an except branch), but a bare `assert`
        # would vanish under `python -O` and leave `None` flowing into the
        # dead-letter record.
        self._dead_letter(
            event,
            topic,
            attempt,
            last_error or RuntimeError("handler failed with no recorded error"),
        )
        return True

    def _dead_letter(
        self, event: EventEnvelope, topic: str, attempts: int, error: Exception
    ) -> None:
        dlq_topic = dlq_for(topic)
        envelope = DeadLetterEnvelope(
            original_topic=topic,
            consumer_group=self.consumer_group,
            attempts=attempts,
            error_type=type(error).__name__,
            error_message=str(error)[:2000],
            original_event=event,
        )
        try:
            self.publisher.publish(
                dlq_topic,
                EventEnvelope(
                    event_type="DEAD_LETTER",
                    source=self.service_name,
                    correlation_id=event.correlation_id,
                    payload=envelope.model_dump(mode="json"),
                ),
                key=str(event.event_id),
            )
        except Exception:
            # If even the DLQ write fails there is nowhere left to put it.
            # Log at error with the full body so it is at least recoverable
            # from the log stream.
            logger.exception(
                "failed to dead-letter event",
                extra={"event_id": str(event.event_id), "payload": event.to_json()},
            )

        KAFKA_EVENTS_FAILED_TOTAL.labels(self.service_name, topic).inc()
        logger.error(
            "event dead-lettered",
            extra={
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "original_topic": topic,
                "dlq_topic": dlq_topic,
                "attempts": attempts,
                "error": str(error),
            },
        )


class KafkaEventConsumer:
    """Long-running consumer loop.

    Runs as its own process, not a thread inside the API. The API scales on
    request latency; a consumer scales on partition count and consumer lag.
    Coupling them would mean scaling the web tier to clear a backlog, and every
    API replica joining the consumer group whether it was needed or not.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        group_id: str,
        topics: list[str],
        service_name: str,
        publisher: EventPublisher,
        retry_policy: RetryPolicy | None = None,
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        from confluent_kafka import Consumer

        self.topics = topics
        self.group_id = group_id
        self.service_name = service_name
        self.poll_timeout_seconds = poll_timeout_seconds
        self._running = False
        self.processor = EventProcessor(
            service_name=service_name,
            consumer_group=group_id,
            publisher=publisher,
            retry_policy=retry_policy,
        )
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                # Manual commits only -- see the module docstring.
                "enable.auto.commit": False,
                # A new consumer group reads the backlog from the beginning
                # rather than skipping whatever happened before it started.
                "auto.offset.reset": "earliest",
                # Generous enough that a slow handler with retries does not get
                # kicked out of the group mid-message, which would cause the
                # event to be redelivered to another consumer.
                "max.poll.interval.ms": 300_000,
                "session.timeout.ms": 45_000,
                "client.id": f"{service_name}-consumer",
            }
        )

    def subscribe(self) -> None:
        self._consumer.subscribe(self.topics)
        logger.info(
            "consumer subscribed",
            extra={"topics": self.topics, "group_id": self.group_id},
        )

    def stop(self, *_: Any) -> None:
        """Ask the loop to finish the current message and exit."""
        logger.info("consumer stopping")
        self._running = False

    def install_signal_handlers(self) -> None:
        """Exit cleanly on SIGTERM so Kubernetes rolling updates do not
        interrupt a message mid-handler."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal received", extra={"signal": signum})
        self.stop()

    def run(self, handler: EventHandler) -> None:  # pragma: no cover - needs a broker
        """Poll, process, commit. Blocks until :meth:`stop` is called."""
        from confluent_kafka import KafkaError

        self.subscribe()
        self._running = True

        try:
            while self._running:
                message = self._consumer.poll(self.poll_timeout_seconds)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("consumer error", extra={"error": str(message.error())})
                    continue

                topic = message.topic()
                try:
                    event = EventEnvelope.from_json(message.value())
                except Exception as exc:
                    # Unparseable: no envelope, so it cannot be retried or
                    # correlated. Commit past it so one bad byte sequence does
                    # not wedge the partition, and log the raw value.
                    logger.error(
                        "undecodable message committed past",
                        extra={
                            "topic": topic,
                            "error": str(exc),
                            "raw": str(message.value())[:500],
                        },
                    )
                    self._consumer.commit(message=message, asynchronous=False)
                    continue

                if self.processor.process(event, topic, handler):
                    self._consumer.commit(message=message, asynchronous=False)
        finally:
            self._consumer.close()
            logger.info("consumer closed")
