"""Inventory event worker.

Runs as its own process:

    python -m app.worker

**Why not a thread inside the API.** The API scales on request latency; a
consumer scales on partition count and consumer lag. Sharing a process means
scaling the web tier to clear a backlog, and every API replica joining the
consumer group whether it is needed or not. In Kubernetes these are two
Deployments with two independent HPAs.
"""

from __future__ import annotations

import logging
import threading
import time

from app.config import get_settings
from app.deps import get_database
from app.handlers import (
    CONSUMER_GROUP,
    handle_order_created,
    handle_order_shipped,
    handle_payment_failed,
)
from retailpulse_common.events.consumer import KafkaEventConsumer, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.outbox import OutboxRelay
from retailpulse_common.events.producer import KafkaEventPublisher
from retailpulse_common.events.topics import Topic
from retailpulse_common.observability import configure_logging

settings = get_settings()
logger = logging.getLogger("inventory-service")

SERVICE = "inventory-service"

HANDLERS = {
    Topic.ORDER_CREATED: handle_order_created,
    Topic.PAYMENT_FAILED: handle_payment_failed,
    Topic.ORDER_SHIPPED: handle_order_shipped,
}


def build_dispatcher(database, publisher):
    """Route an event to its handler inside one database transaction.

    The transaction boundary is here, not in the handlers: claiming the event
    and applying its side effects must commit together or not at all.
    """

    def dispatch(event: EventEnvelope, topic: str) -> None:
        handler = HANDLERS.get(topic)
        if handler is None:
            logger.warning("no handler for topic", extra={"topic": topic})
            return
        with database.session() as session:
            handler(event, topic, session=session, publisher=publisher)

    return dispatch


def start_outbox_relay(database, publisher, interval_seconds: float = 1.0) -> threading.Thread:
    """Background thread draining this service's outbox."""

    def loop() -> None:
        while True:
            try:
                if OutboxRelay(database, publisher).publish_pending() == 0:
                    time.sleep(interval_seconds)
            except Exception:
                logger.exception("outbox relay iteration failed")
                time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="outbox-relay", daemon=True)
    thread.start()
    return thread


def main() -> None:  # pragma: no cover - process entrypoint
    configure_logging(SERVICE, settings.log_level)
    database = get_database()
    publisher = KafkaEventPublisher(settings.kafka_bootstrap_servers, SERVICE)

    start_outbox_relay(database, publisher)

    consumer = KafkaEventConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=CONSUMER_GROUP,
        topics=list(HANDLERS),
        service_name=SERVICE,
        publisher=publisher,
        retry_policy=RetryPolicy(
            max_attempts=settings.kafka_max_retries,
            backoff_seconds=settings.kafka_retry_backoff_seconds,
        ),
    )
    consumer.install_signal_handlers()

    logger.info("inventory worker starting", extra={"topics": list(HANDLERS)})
    consumer.run(build_dispatcher(database, publisher))


if __name__ == "__main__":  # pragma: no cover
    main()
