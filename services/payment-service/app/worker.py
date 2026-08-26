"""Payment event worker.

    python -m app.worker
"""

from __future__ import annotations

import logging
import threading
import time

from app.config import get_settings
from app.deps import get_database, get_gateway
from app.handlers import CONSUMER_GROUP, HANDLERS
from retailpulse_common.events.consumer import KafkaEventConsumer, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.outbox import OutboxRelay
from retailpulse_common.events.producer import KafkaEventPublisher
from retailpulse_common.observability import configure_logging

settings = get_settings()
logger = logging.getLogger("payment-service")

SERVICE = "payment-service"


def build_dispatcher(database, gateway):
    """Route an event to its handler inside one database transaction.

    The charge, the payment row and the outbox entry announcing it all commit
    together.
    """

    def dispatch(event: EventEnvelope, topic: str) -> None:
        handler = HANDLERS.get(topic)
        if handler is None:
            logger.warning("no handler for topic", extra={"topic": topic})
            return
        with database.session() as session:
            handler(event, topic, session=session, gateway=gateway)

    return dispatch


def start_outbox_relay(database, publisher, interval_seconds: float = 1.0) -> threading.Thread:
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

    logger.info(
        "payment worker starting",
        extra={"topics": list(HANDLERS), "success_rate": settings.payment_success_rate},
    )
    consumer.run(build_dispatcher(database, get_gateway()))


if __name__ == "__main__":  # pragma: no cover
    main()
