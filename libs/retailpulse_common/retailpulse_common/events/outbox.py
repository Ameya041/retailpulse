"""The transactional outbox.

## The dual-write problem

Creating an order means two writes to two different systems: a row in Postgres
and a message in Kafka. There is no transaction spanning both, so whichever
order you choose, a crash in between breaks something:

*Publish first, then commit* -- if the commit fails, downstream services reserve
stock for an order that does not exist.

*Commit first, then publish* -- if the publish fails (broker down, pod killed,
network blip), the order exists but nothing downstream ever hears about it. It
sits in CREATED forever. This is the more common failure and the more insidious
one, because everything looks fine until a customer complains.

Wrapping the publish in a try/except does not fix it. The process can die
between the two statements, and no exception handler runs for a SIGKILL.

## The fix

Write the event **into the same database, in the same transaction** as the
business data:

    BEGIN
      INSERT INTO orders ...
      INSERT INTO outbox_events ...
    COMMIT

Now there is exactly one write, to one system, and it is atomic. Either the
order and its pending event both exist, or neither does.

A separate **relay** then polls the outbox, publishes to Kafka, and marks rows
published. If the relay crashes mid-flight, the row is still unpublished and
gets picked up next pass. That means an event may be published *more than
once* -- which is fine, because every consumer already deduplicates on
``event_id``. At-least-once plus idempotent consumers gives effectively-once
processing.

## Cost, stated honestly

This adds latency (an event is published on the next poll, not instantly) and a
process to operate. The alternative used in larger systems is change-data
capture reading the Postgres WAL directly (Debezium), which removes the polling
but adds a much heavier piece of infrastructure. Polling is the right call at
this scale.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from retailpulse_common.db import Base, Database
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.producer import EventPublisher

logger = logging.getLogger(__name__)


class OutboxEvent(Base):
    """An event awaiting publication, written with the business data."""

    __tablename__ = "outbox_events"

    outbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # The fully serialised envelope. Storing the finished JSON means the relay
    # needs no knowledge of any event's shape.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The relay's only query: "unpublished, oldest first". A partial index
        # would be even tighter on Postgres, but this stays portable and the
        # table is kept small by pruning.
        Index("ix_outbox_events_unpublished", "published_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OutboxEvent {self.topic} published={self.published_at is not None}>"


def enqueue(
    session: Session, topic: str, event: EventEnvelope, *, key: str | None = None
) -> OutboxEvent:
    """Stage an event for publication on the caller's transaction.

    Deliberately does not talk to Kafka. It only writes a row -- which is what
    makes it atomic with the business data.
    """
    record = OutboxEvent(
        event_id=event.event_id,
        topic=topic,
        partition_key=key or str(event.event_id),
        body=event.to_json(),
    )
    session.add(record)
    return record


class OutboxRelay:
    """Polls the outbox and publishes to Kafka.

    Runs inside the service's worker process. Safe to run in more than one
    replica: rows are claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``, so two
    relays never grab the same row, and neither blocks the other.
    """

    def __init__(
        self,
        database: Database,
        publisher: EventPublisher,
        *,
        batch_size: int = 100,
        max_attempts: int = 10,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.batch_size = batch_size
        self.max_attempts = max_attempts

    def publish_pending(self) -> int:
        """Publish one batch. Returns how many were published."""
        published = 0

        with self.database.session() as session:
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .where(OutboxEvent.attempts < self.max_attempts)
                .order_by(OutboxEvent.created_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list(session.scalars(stmt).all())

            for row in rows:
                row.attempts += 1
                try:
                    self.publisher.publish(
                        row.topic,
                        EventEnvelope.from_json(row.body),
                        key=row.partition_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Leave it unpublished; the next pass retries it. The
                    # attempt counter stops a permanently broken row from being
                    # retried forever.
                    row.last_error = str(exc)[:2000]
                    logger.warning(
                        "outbox publish failed",
                        extra={
                            "topic": row.topic,
                            "attempts": row.attempts,
                            "error": str(exc),
                        },
                    )
                    continue

                row.published_at = datetime.now(UTC)
                row.last_error = None
                published += 1

        if published:
            logger.info("outbox batch published", extra={"count": published})
        return published

    def prune(self, older_than: timedelta = timedelta(days=7)) -> int:
        """Delete published rows past their retention window.

        Without this the outbox grows forever and its index degrades. Only
        *published* rows are removed -- an unpublished row is still owed to the
        rest of the system, no matter how old.
        """
        cutoff = datetime.now(UTC) - older_than
        with self.database.session() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_not(None))
                .where(OutboxEvent.published_at < cutoff)
                .limit(1000)
            ).all()
            for row in rows:
                session.delete(row)
            return len(rows)

    def stuck_events(self) -> list[OutboxEvent]:
        """Rows that exhausted their attempts and need a human.

        Surfaced by the worker's health endpoint: a non-empty result means
        events are being silently withheld from the rest of the platform.
        """
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .where(OutboxEvent.attempts >= self.max_attempts)
                    .limit(100)
                ).all()
            )
