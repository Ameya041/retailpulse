"""Idempotent event processing.

## The problem

Kafka gives **at-least-once** delivery. A consumer that crashes after doing its
work but before committing its offset will see the same event again on restart.
Rebalances cause the same thing. This is not an edge case -- it is normal
operation, and it will happen in production.

If ORDER_CREATED is processed twice, the naive outcome is two reservations for
one order, or two confirmation emails.

## Why not exactly-once delivery

Kafka does offer transactional exactly-once *within Kafka*. It does not extend
to a Postgres write in another system, which is where the side effects actually
land. So the delivery guarantee stays at-least-once and the *effect* is made
idempotent instead -- which is simpler to reason about and works regardless of
what the broker does.

## The mechanism

A ``processed_events`` row is written **inside the same database transaction as
the handler's side effects**. Either both commit or neither does. There is no
window in which the work happened but the event looks unprocessed, or vice
versa.

The primary key does the enforcing:

    PRIMARY KEY (event_id, consumer_group)

Not an application-level "have I seen this?" check -- that reads, then writes,
and two consumers in the same group can interleave between the two steps. A
primary key is evaluated atomically by the database, so the duplicate loses.

``consumer_group`` is part of the key because different services legitimately
process the same event. The order service and the analytics service both care
about ORDER_CREATED; neither should suppress the other.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from retailpulse_common.db import Base

logger = logging.getLogger(__name__)


class ProcessedEvent(Base):
    """One row per (event, consumer group) successfully handled."""

    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    # Recorded so a replay can be reasoned about after the fact.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_note: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_processed_events_processed_at", "processed_at"),
        Index("ix_processed_events_event_type", "event_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProcessedEvent {self.event_type} {self.event_id}>"


class DuplicateEventError(Exception):
    """Raised when an event has already been processed by this consumer group."""

    def __init__(self, event_id: uuid.UUID, consumer_group: str) -> None:
        self.event_id = event_id
        self.consumer_group = consumer_group
        super().__init__(
            f"Event {event_id} was already processed by consumer group "
            f"{consumer_group!r}."
        )


class IdempotencyGuard:
    """Claims events for a consumer group inside the caller's transaction."""

    def __init__(self, session: Session, consumer_group: str) -> None:
        self.session = session
        self.consumer_group = consumer_group

    def claim(
        self,
        *,
        event_id: uuid.UUID,
        event_type: str,
        topic: str,
        correlation_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> None:
        """Reserve this event for processing, or raise :class:`DuplicateEventError`.

        Call this **before** the side effects, on the same session. The insert
        is flushed immediately so a duplicate is detected up front rather than
        after the work has been done.

        Note this does not commit: the caller's transaction commits the claim
        and the side effects together, which is the whole point.
        """
        record = ProcessedEvent(
            event_id=event_id,
            consumer_group=self.consumer_group,
            event_type=event_type,
            topic=topic,
            correlation_id=correlation_id,
            result_note=note,
        )
        try:
            # A SAVEPOINT, so that losing this race does not poison the outer
            # transaction -- the caller can carry on and simply skip the event.
            #
            # The `add` must happen *inside* the nested block: an object added
            # to the outer transaction survives the savepoint rollback and the
            # next flush would retry the same failing INSERT.
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError as exc:
            # No cleanup needed: rolling back to the savepoint also evicts the
            # rejected instance from the session, so it cannot be flushed again.
            logger.info(
                "duplicate event skipped",
                extra={
                    "event_id": str(event_id),
                    "event_type": event_type,
                    "consumer_group": self.consumer_group,
                },
            )
            raise DuplicateEventError(event_id, self.consumer_group) from exc

    def has_processed(self, event_id: uuid.UUID) -> bool:
        """Read-only check.

        Useful for diagnostics and tests. Do **not** branch on this before
        doing work -- the read and the subsequent write are not atomic
        together, which is exactly the race :meth:`claim` avoids.
        """
        return (
            self.session.get(ProcessedEvent, (event_id, self.consumer_group)) is not None
        )
