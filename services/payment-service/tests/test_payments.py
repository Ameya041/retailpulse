"""Payment service tests.

The property these exist to protect above all others: **a customer is never
charged twice for one order.**
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.gateway import (
    AlwaysApprovesGateway,
    AlwaysDeclinesGateway,
    SimulatedPaymentGateway,
    UnavailableGateway,
)
from app.handlers import CONSUMER_GROUP, handle_order_cancelled, handle_payment_requested
from app.models import Payment, PaymentStatus
from app.service import PaymentService
from retailpulse_common.errors import ConflictError, NotFoundError, ValidationError
from retailpulse_common.events.consumer import EventProcessor, PermanentEventError, RetryPolicy
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import DuplicateEventError, IdempotencyGuard
from retailpulse_common.events.outbox import OutboxEvent
from retailpulse_common.events.producer import InMemoryEventPublisher
from retailpulse_common.events.topics import EventType, Topic
from tests.conftest import CUSTOMER_ID


def payment_requested(order_id, amount="199.99", customer_id=None) -> EventEnvelope:
    return EventEnvelope(
        event_type=EventType.PAYMENT_REQUESTED,
        source="order-service",
        payload={
            "order_id": str(order_id),
            "amount": amount,
            "currency": "INR",
            "customer_id": str(customer_id) if customer_id else None,
        },
    )


def _outbox_topics(database) -> list[str]:
    with database.session() as session:
        return [row.topic for row in session.query(OutboxEvent).all()]


# ---------------------------------------------------------------------------
# The gateway simulation
# ---------------------------------------------------------------------------
def test_simulated_gateway_respects_its_success_rate():
    """Statistical, but with a fixed seed so it cannot flake."""
    gateway = SimulatedPaymentGateway(success_rate=0.95, seed=42)

    results = [
        gateway.charge(
            order_id=uuid.uuid4(), amount=Decimal("100"), currency="INR", method="CARD"
        )
        for _ in range(1000)
    ]
    approved = sum(1 for r in results if r.approved)

    assert 930 <= approved <= 970  # ~95% with sampling slack


def test_gateway_can_be_configured_to_always_fail():
    gateway = SimulatedPaymentGateway(success_rate=0.0, seed=1)
    result = gateway.charge(
        order_id=uuid.uuid4(), amount=Decimal("10"), currency="INR", method="CARD"
    )
    assert result.approved is False
    assert result.failure_reason


def test_gateway_seed_makes_outcomes_reproducible():
    """A demo must be repeatable."""
    a = SimulatedPaymentGateway(success_rate=0.5, seed=7)
    b = SimulatedPaymentGateway(success_rate=0.5, seed=7)
    order_id = uuid.uuid4()

    first = [a.charge(order_id=order_id, amount=Decimal("1"), currency="INR", method="CARD").approved for _ in range(20)]
    second = [b.charge(order_id=order_id, amount=Decimal("1"), currency="INR", method="CARD").approved for _ in range(20)]

    assert first == second


def test_invalid_success_rate_is_rejected():
    with pytest.raises(ValueError):
        SimulatedPaymentGateway(success_rate=1.5)


def test_declines_carry_a_reason():
    result = AlwaysDeclinesGateway("CARD_EXPIRED").charge(
        order_id=uuid.uuid4(), amount=Decimal("1"), currency="INR", method="CARD"
    )
    assert result.failure_reason == "CARD_EXPIRED"


def test_transaction_references_are_unique():
    gateway = AlwaysApprovesGateway()
    refs = {
        gateway.charge(
            order_id=uuid.uuid4(), amount=Decimal("1"), currency="INR", method="CARD"
        ).transaction_reference
        for _ in range(500)
    }
    assert len(refs) == 500


# ---------------------------------------------------------------------------
# Charging
# ---------------------------------------------------------------------------
def test_successful_charge_is_recorded(session, gateway):
    payment = PaymentService(session, gateway).charge(
        order_id=uuid.uuid4(), amount=Decimal("199.99")
    )

    assert payment.status == PaymentStatus.SUCCESS.value
    assert payment.amount == Decimal("199.99")
    assert payment.transaction_reference.startswith("TXN-")
    assert payment.failure_reason is None


def test_declined_charge_is_recorded_not_raised(session, declining_gateway):
    """A decline is an outcome, not an exception."""
    payment = PaymentService(session, declining_gateway).charge(
        order_id=uuid.uuid4(), amount=Decimal("50.00")
    )

    assert payment.status == PaymentStatus.FAILED.value
    assert payment.failure_reason == "INSUFFICIENT_FUNDS"


def test_provider_outage_raises_so_the_consumer_retries(session):
    """An outage is not an answer, so it must not be recorded as a decline."""
    with pytest.raises(ConnectionError):
        PaymentService(session, UnavailableGateway()).charge(
            order_id=uuid.uuid4(), amount=Decimal("10.00")
        )


def test_nothing_is_written_when_the_provider_is_unreachable(session, database):
    order_id = uuid.uuid4()
    with pytest.raises(ConnectionError):
        PaymentService(session, UnavailableGateway()).charge(
            order_id=order_id, amount=Decimal("10.00")
        )

    # No orphan PENDING row to reconcile later.
    assert PaymentService(session, UnavailableGateway()).find_by_order(order_id) is None


def test_charging_the_same_order_twice_returns_the_first_payment(session, gateway):
    """The core guarantee: one charge per order."""
    order_id = uuid.uuid4()
    service = PaymentService(session, gateway)

    first = service.charge(order_id=order_id, amount=Decimal("100.00"))
    second = service.charge(order_id=order_id, amount=Decimal("100.00"))

    assert first.payment_id == second.payment_id
    assert first.transaction_reference == second.transaction_reference
    assert session.query(Payment).count() == 1


def test_unique_index_blocks_a_second_payment_row(session, gateway):
    """Backstop: even a direct write cannot double-charge."""
    from sqlalchemy.exc import IntegrityError

    order_id = uuid.uuid4()
    PaymentService(session, gateway).charge(order_id=order_id, amount=Decimal("10.00"))

    session.add(
        Payment(
            order_id=order_id,
            amount=Decimal("10.00"),
            currency="INR",
            status=PaymentStatus.SUCCESS.value,
            transaction_reference="TXN-DUPLICATE-ATTEMPT",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_zero_amount_is_rejected(session, gateway):
    with pytest.raises(ValidationError):
        PaymentService(session, gateway).charge(order_id=uuid.uuid4(), amount=Decimal("0"))


def test_amount_keeps_exact_decimal_precision(session, gateway):
    payment = PaymentService(session, gateway).charge(
        order_id=uuid.uuid4(), amount=Decimal("1234.56")
    )
    assert Decimal(str(payment.amount)) == Decimal("1234.56")


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
def test_full_refund(session, gateway):
    order_id = uuid.uuid4()
    service = PaymentService(session, gateway)
    service.charge(order_id=order_id, amount=Decimal("500.00"))

    payment = service.refund(order_id, reason="RETURNED")

    assert payment.status == PaymentStatus.REFUNDED.value
    assert payment.refunded_amount == Decimal("500.00")


def test_partial_refund(session, gateway):
    order_id = uuid.uuid4()
    service = PaymentService(session, gateway)
    service.charge(order_id=order_id, amount=Decimal("500.00"))

    payment = service.refund(order_id, amount=Decimal("120.00"), reason="ONE_ITEM_RETURNED")

    assert payment.refunded_amount == Decimal("120.00")


def test_refund_cannot_exceed_the_charge(session, gateway):
    order_id = uuid.uuid4()
    service = PaymentService(session, gateway)
    service.charge(order_id=order_id, amount=Decimal("100.00"))

    with pytest.raises(ValidationError):
        service.refund(order_id, amount=Decimal("150.00"), reason="TOO_MUCH")


def test_a_failed_payment_cannot_be_refunded(session, declining_gateway):
    order_id = uuid.uuid4()
    service = PaymentService(session, declining_gateway)
    service.charge(order_id=order_id, amount=Decimal("100.00"))

    with pytest.raises(ConflictError):
        service.refund(order_id, reason="NOTHING_TO_REFUND")


def test_double_refund_is_rejected(session, gateway):
    order_id = uuid.uuid4()
    service = PaymentService(session, gateway)
    service.charge(order_id=order_id, amount=Decimal("100.00"))
    service.refund(order_id, reason="FIRST")

    with pytest.raises(ConflictError):
        service.refund(order_id, reason="SECOND")


def test_refunding_an_unknown_order_is_404(session, gateway):
    with pytest.raises(NotFoundError):
        PaymentService(session, gateway).refund(uuid.uuid4(), reason="X")


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------
def test_payment_requested_charges_and_stages_confirmed(database, gateway):
    order_id = uuid.uuid4()
    event = payment_requested(order_id, "250.00", CUSTOMER_ID)

    with database.session() as session:
        handle_payment_requested(event, Topic.PAYMENT_REQUESTED, session=session, gateway=gateway)

    assert _outbox_topics(database) == [Topic.PAYMENT_CONFIRMED]
    with database.session() as session:
        payment = PaymentService(session, gateway).get_by_order(order_id)
        assert payment.status == PaymentStatus.SUCCESS.value
        assert payment.customer_id == CUSTOMER_ID


def test_declined_payment_stages_payment_failed(database, declining_gateway):
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_payment_requested(
            payment_requested(order_id),
            Topic.PAYMENT_REQUESTED,
            session=session,
            gateway=declining_gateway,
        )

    assert _outbox_topics(database) == [Topic.PAYMENT_FAILED]
    with database.session() as session:
        body = session.query(OutboxEvent).one().body
        assert "INSUFFICIENT_FUNDS" in body


def test_outbox_entry_and_payment_commit_together(database):
    """A charge with no announcement would strand the order."""
    order_id = uuid.uuid4()

    with pytest.raises(RuntimeError), database.session() as session:
        handle_payment_requested(
            payment_requested(order_id),
            Topic.PAYMENT_REQUESTED,
            session=session,
            gateway=AlwaysApprovesGateway(),
        )
        raise RuntimeError("failure after the handler")

    with database.session() as session:
        assert session.query(Payment).count() == 0
        assert session.query(OutboxEvent).count() == 0


def test_redelivered_payment_request_does_not_charge_twice(database, gateway):
    """The whole reason processed_events exists."""
    order_id = uuid.uuid4()
    event = payment_requested(order_id)

    with database.session() as session:
        handle_payment_requested(event, Topic.PAYMENT_REQUESTED, session=session, gateway=gateway)

    with pytest.raises(DuplicateEventError), database.session() as session:
        handle_payment_requested(event, Topic.PAYMENT_REQUESTED, session=session, gateway=gateway)

    with database.session() as session:
        assert session.query(Payment).count() == 1
        assert session.query(OutboxEvent).count() == 1


def test_a_different_event_for_the_same_order_still_only_charges_once(database, gateway):
    """Defence in depth: a fresh event_id gets past the idempotency table,
    and the unique index on order_id catches it anyway."""
    order_id = uuid.uuid4()

    for _ in range(3):
        with database.session() as session:
            handle_payment_requested(
                payment_requested(order_id),
                Topic.PAYMENT_REQUESTED,
                session=session,
                gateway=gateway,
            )

    with database.session() as session:
        assert session.query(Payment).count() == 1


def test_provider_outage_is_retried_then_dead_lettered(database):
    publisher = InMemoryEventPublisher()
    processor = EventProcessor(
        service_name="payment-service",
        consumer_group=CONSUMER_GROUP,
        publisher=publisher,
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=lambda _: None,
    )

    def dispatch(evt, topic):
        with database.session() as session:
            handle_payment_requested(
                evt, topic, session=session, gateway=UnavailableGateway()
            )

    assert processor.process(payment_requested(uuid.uuid4()), Topic.PAYMENT_REQUESTED, dispatch)

    from retailpulse_common.events.topics import DeadLetterTopic

    assert publisher.topics() == [DeadLetterTopic.PAYMENTS]
    with database.session() as session:
        assert session.query(Payment).count() == 0


def test_missing_amount_fails_permanently(database, gateway):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_payment_requested(
            EventEnvelope(
                event_type=EventType.PAYMENT_REQUESTED,
                source="order-service",
                payload={"order_id": str(uuid.uuid4())},
            ),
            Topic.PAYMENT_REQUESTED,
            session=session,
            gateway=gateway,
        )


def test_negative_amount_fails_permanently(database, gateway):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_payment_requested(
            payment_requested(uuid.uuid4(), "-10.00"),
            Topic.PAYMENT_REQUESTED,
            session=session,
            gateway=gateway,
        )


def test_non_numeric_amount_fails_permanently(database, gateway):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_payment_requested(
            payment_requested(uuid.uuid4(), "not-a-number"),
            Topic.PAYMENT_REQUESTED,
            session=session,
            gateway=gateway,
        )


def test_cancelling_a_paid_order_auto_refunds(database, gateway):
    order_id = uuid.uuid4()
    with database.session() as session:
        handle_payment_requested(
            payment_requested(order_id), Topic.PAYMENT_REQUESTED, session=session, gateway=gateway
        )

    with database.session() as session:
        handle_order_cancelled(
            EventEnvelope(
                event_type=EventType.ORDER_CANCELLED,
                source="order-service",
                payload={"order_id": str(order_id), "reason": "OUT_OF_STOCK"},
            ),
            Topic.ORDER_CANCELLED,
            session=session,
            gateway=gateway,
        )

    with database.session() as session:
        payment = PaymentService(session, gateway).get_by_order(order_id)
        assert payment.status == PaymentStatus.REFUNDED.value
        assert payment.refunded_amount == payment.amount


def test_cancelling_an_unpaid_order_is_a_no_op(database, gateway):
    with database.session() as session:
        handle_order_cancelled(
            EventEnvelope(
                event_type=EventType.ORDER_CANCELLED,
                source="order-service",
                payload={"order_id": str(uuid.uuid4())},
            ),
            Topic.ORDER_CANCELLED,
            session=session,
            gateway=gateway,
        )

    with database.session() as session:
        assert session.query(Payment).count() == 0


def test_processed_events_is_scoped_to_this_consumer_group(database, gateway):
    order_id = uuid.uuid4()
    event = payment_requested(order_id)
    with database.session() as session:
        handle_payment_requested(event, Topic.PAYMENT_REQUESTED, session=session, gateway=gateway)

    with database.session() as session:
        assert IdempotencyGuard(session, CONSUMER_GROUP).has_processed(event.event_id)
        assert not IdempotencyGuard(session, "analytics-service").has_processed(event.event_id)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_charge_endpoint_requires_admin(client, customer_headers):
    response = client.post(
        "/payments",
        json={"order_id": str(uuid.uuid4()), "amount": "10.00"},
        headers=customer_headers,
    )
    assert response.status_code == 403


def test_charge_endpoint_is_idempotent(client, admin_headers):
    order_id = str(uuid.uuid4())
    payload = {"order_id": order_id, "amount": "99.00"}

    first = client.post("/payments", json=payload, headers=admin_headers).json()
    second = client.post("/payments", json=payload, headers=admin_headers).json()

    assert first["payment_id"] == second["payment_id"]


def test_customer_can_see_their_own_payment(client, admin_headers, customer_headers):
    order_id = str(uuid.uuid4())
    client.post(
        "/payments",
        json={"order_id": order_id, "amount": "42.00", "customer_id": str(CUSTOMER_ID)},
        headers=admin_headers,
    )

    response = client.get(f"/payments/{order_id}", headers=customer_headers)

    assert response.status_code == 200
    assert response.json()["amount"] == "42.00"


def test_another_customers_payment_returns_404(client, admin_headers, other_customer_headers):
    order_id = str(uuid.uuid4())
    client.post(
        "/payments",
        json={"order_id": order_id, "amount": "42.00", "customer_id": str(CUSTOMER_ID)},
        headers=admin_headers,
    )

    response = client.get(f"/payments/{order_id}", headers=other_customer_headers)

    assert response.status_code == 404


def test_refund_endpoint_requires_admin(client, admin_headers, customer_headers):
    order_id = str(uuid.uuid4())
    client.post("/payments", json={"order_id": order_id, "amount": "10.00"}, headers=admin_headers)

    response = client.post(
        f"/payments/{order_id}/refund", json={"reason": "MINE_NOW"}, headers=customer_headers
    )
    assert response.status_code == 403


def test_refund_endpoint(client, admin_headers):
    order_id = str(uuid.uuid4())
    client.post("/payments", json={"order_id": order_id, "amount": "80.00"}, headers=admin_headers)

    response = client.post(
        f"/payments/{order_id}/refund", json={"reason": "RETURNED"}, headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REFUNDED"


def test_lookup_by_transaction_reference(client, admin_headers):
    order_id = str(uuid.uuid4())
    created = client.post(
        "/payments", json={"order_id": order_id, "amount": "10.00"}, headers=admin_headers
    ).json()

    response = client.get(
        f"/payments/reference/{created['transaction_reference']}", headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["order_id"] == order_id


def test_stats_reports_the_approval_rate(client, admin_headers):
    for _ in range(4):
        client.post(
            "/payments",
            json={"order_id": str(uuid.uuid4()), "amount": "10.00"},
            headers=admin_headers,
        )

    body = client.get("/payments/stats", headers=admin_headers).json()

    assert body["total_payments"] == 4
    assert body["successful"] == 4
    assert body["success_rate"] == 1.0
    assert body["total_collected"] == "40.00"


def test_refunded_payments_still_count_as_approved(client, admin_headers):
    order_id = str(uuid.uuid4())
    client.post("/payments", json={"order_id": order_id, "amount": "10.00"}, headers=admin_headers)
    refund = client.post(
        f"/payments/{order_id}/refund", json={"reason": "RETURNED"}, headers=admin_headers
    )
    assert refund.status_code == 200

    body = client.get("/payments/stats", headers=admin_headers).json()

    assert body["refunded"] == 1
    assert body["success_rate"] == 1.0  # the gateway did approve it


def test_unknown_payment_returns_404(client, admin_headers):
    assert client.get(f"/payments/{uuid.uuid4()}", headers=admin_headers).status_code == 404


def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "payment-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/payments/{order_id}/refund" in paths
