"""Analytics event worker.

    python -m app.worker

Consumes order lifecycle events into the read model. Publishes nothing.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.deps import get_database
from app.handlers import CONSUMER_GROUP, HANDLERS
from retailpulse_common.events.consumer import KafkaEventConsumer, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.producer import KafkaEventPublisher
from retailpulse_common.observability import configure_logging

settings = get_settings()
logger = logging.getLogger("analytics-service")

SERVICE = "analytics-service"


def build_dispatcher(database):
    def dispatch(event: EventEnvelope, topic: str) -> None:
        handler = HANDLERS.get(topic)
        if handler is None:
            logger.warning("no handler for topic", extra={"topic": topic})
            return
        with database.session() as session:
            handler(event, topic, session=session)

    return dispatch


def main() -> None:  # pragma: no cover - process entrypoint
    configure_logging(SERVICE, settings.log_level)
    database = get_database()

    # This service publishes no business events, but the consumer still needs a
    # producer to write dead letters.
    publisher = KafkaEventPublisher(settings.kafka_bootstrap_servers, SERVICE)

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

    logger.info("analytics worker starting", extra={"topics": list(HANDLERS)})
    consumer.run(build_dispatcher(database))


if __name__ == "__main__":  # pragma: no cover
    main()
