"""The event envelope every service publishes and consumes.

One shape for every event on every topic. Consumers can therefore parse,
log, deduplicate and route a message without knowing anything about its
payload, and a new event type needs no new plumbing.

Field-by-field, and why each earns its place:

``event_id``
    A UUID assigned by the producer. This is the deduplication key. Kafka
    guarantees at-least-once delivery, so a consumer *will* eventually see the
    same event twice; without a stable ID there is no way to notice.

``event_type``
    The business meaning (``ORDER_CREATED``). Deliberately separate from the
    topic name, so one topic can carry related event types and a consumer can
    branch without inspecting the payload.

``timestamp``
    When the event *happened*, set by the producer -- not when it was consumed.
    Consumer-side clocks would make ordering meaningless after a replay.

``source``
    The service that emitted it. The first question about a bad event is
    always "who published this?".

``version``
    Schema version of the payload. Lets a consumer keep handling v1 while
    producers roll out v2, which is what makes independent deploys possible.

``correlation_id``
    Shared by every event in one business transaction. An order flows through
    six services; this is what stitches those log lines back into one story.

``payload``
    The event-specific body. Everything above is infrastructure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventEnvelope(BaseModel):
    """A single event in transit."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    timestamp: datetime = Field(default_factory=_utc_now)
    source: str
    version: int = 1
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> EventEnvelope:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return cls.model_validate_json(raw)

    def child(self, *, event_type: str, source: str, payload: dict[str, Any]) -> EventEnvelope:
        """Derive a follow-on event that stays in the same correlation chain.

        ORDER_CREATED causes INVENTORY_RESERVED causes PAYMENT_CONFIRMED. Each
        is a new event with a new ``event_id``, but all three carry the same
        ``correlation_id`` so the whole saga can be traced from one filter.
        """
        return EventEnvelope(
            event_type=event_type,
            source=source,
            correlation_id=self.correlation_id,
            payload=payload,
        )


class DeadLetterEnvelope(BaseModel):
    """Wrapper written to a dead-letter topic when an event cannot be processed.

    The original event is preserved verbatim so it can be replayed after a fix,
    and the failure context is recorded alongside it -- a DLQ entry that only
    says "it failed" forces you back into the logs of a pod that has since been
    replaced.
    """

    model_config = ConfigDict(extra="forbid")

    dead_lettered_at: datetime = Field(default_factory=_utc_now)
    original_topic: str
    consumer_group: str
    attempts: int
    error_type: str
    error_message: str
    original_event: EventEnvelope

    def to_json(self) -> str:
        return self.model_dump_json()
