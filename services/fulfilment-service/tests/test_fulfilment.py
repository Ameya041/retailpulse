"""Fulfilment service tests."""

from __future__ import annotations

import uuid

import pytest

from app.handlers import handle_order_confirmed
from app.models import Fulfilment, FulfilmentStatus
from app.service import MAX_DELIVERY_ATTEMPTS, FulfilmentService, InvalidFulfilmentTransition
from retailpulse_common.errors import NotFoundError
from retailpulse_common.events.consumer import PermanentEventError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.idempotency import DuplicateEventError
from retailpulse_common.events.outbox import OutboxEvent
from retailpulse_common.events.topics import EventType, Topic
from tests.conftest import CUSTOMER_ID, SHIPPING_ADDRESS, advance


def order_confirmed(order_id, address=SHIPPING_ADDRESS, customer_id=CUSTOMER_ID) -> EventEnvelope:
    payload = {"order_id": str(order_id), "shipping_address": address}
    if customer_id:
        payload["customer_id"] = str(customer_id)
    return EventEnvelope(
        event_type=EventType.ORDER_CONFIRMED, source="order-service", payload=payload
    )


def _outbox_topics(database) -> list[str]:
    with database.session() as session:
        return [row.topic for row in session.query(OutboxEvent).all()]


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
def test_happy_path_through_every_state(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)

    service.begin_picking(order_id)
    service.mark_packed(order_id)
    shipped = service.ship(order_id)
    assert shipped.status == FulfilmentStatus.SHIPPED.value
    assert shipped.tracking_number
    assert shipped.carrier
    assert shipped.shipped_at

    delivered = service.deliver(order_id)
    assert delivered.status == FulfilmentStatus.DELIVERED.value
    assert delivered.delivered_at
    assert delivered.delivery_attempts == 1


def test_cannot_ship_before_packing(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)

    with pytest.raises(InvalidFulfilmentTransition):
        service.ship(order_id)


def test_cannot_deliver_before_shipping(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)

    with pytest.raises(InvalidFulfilmentTransition):
        service.deliver(order_id)


def test_delivered_is_terminal(session, rng):
    """There is no cancellation edge anywhere in this machine."""
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    service.ship(order_id)
    service.deliver(order_id)

    with pytest.raises(InvalidFulfilmentTransition):
        service.fail_delivery(order_id, "TOO_LATE")


def test_illegal_transition_names_what_is_allowed(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)

    with pytest.raises(InvalidFulfilmentTransition) as exc:
        service.deliver(order_id)

    assert exc.value.details["current_status"] == "PENDING"
    assert exc.value.details["allowed_next"] == ["PICKING"]


def test_repeating_a_transition_is_an_idempotent_no_op(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    first = service.ship(order_id)

    second = service.ship(order_id)

    assert second.tracking_number == first.tracking_number
    assert second.shipped_at == first.shipped_at


# ---------------------------------------------------------------------------
# Failed delivery and redelivery
# ---------------------------------------------------------------------------
def test_failed_delivery_can_be_reattempted_with_the_same_tracking_number(session, rng):
    """It is the same physical parcel; a customer watching the old number
    must not lose sight of it."""
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    original = service.ship(order_id).tracking_number

    service.fail_delivery(order_id, "RECIPIENT_UNAVAILABLE")
    reshipped = service.ship(order_id)

    assert reshipped.status == FulfilmentStatus.SHIPPED.value
    assert reshipped.tracking_number == original
    assert reshipped.failure_reason is None


def test_delivery_attempts_are_counted(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    service.ship(order_id)

    service.fail_delivery(order_id, "NOBODY_HOME")
    service.ship(order_id)
    fulfilment = service.fail_delivery(order_id, "NOBODY_HOME_AGAIN")

    assert fulfilment.delivery_attempts == 2
    assert service.can_reattempt(fulfilment) is True


def test_reattempts_are_bounded(session, rng):
    """After the limit the parcel needs a human, not another van."""
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    service.ship(order_id)

    for _ in range(MAX_DELIVERY_ATTEMPTS):
        service.fail_delivery(order_id, "NOBODY_HOME")
        if service.can_reattempt(service.get(order_id)):
            service.ship(order_id)

    assert service.can_reattempt(service.get(order_id)) is False


def test_delivery_after_a_failed_attempt_succeeds(session, rng):
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()
    service.start(order_id, SHIPPING_ADDRESS)
    service.begin_picking(order_id)
    service.mark_packed(order_id)
    service.ship(order_id)
    service.fail_delivery(order_id, "NOBODY_HOME")
    service.ship(order_id)

    delivered = service.deliver(order_id)

    assert delivered.status == FulfilmentStatus.DELIVERED.value
    assert delivered.delivery_attempts == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_starting_the_same_order_twice_returns_one_fulfilment(session, rng):
    """A redelivered ORDER_CONFIRMED must not dispatch the same goods twice."""
    service = FulfilmentService(session, rng)
    order_id = uuid.uuid4()

    first = service.start(order_id, SHIPPING_ADDRESS)
    second = service.start(order_id, SHIPPING_ADDRESS)

    assert first.fulfilment_id == second.fulfilment_id
    assert session.query(Fulfilment).count() == 1


def test_unique_index_blocks_a_second_fulfilment_row(session, rng):
    from sqlalchemy.exc import IntegrityError

    order_id = uuid.uuid4()
    FulfilmentService(session, rng).start(order_id, SHIPPING_ADDRESS)

    session.add(Fulfilment(order_id=order_id, shipping_address=SHIPPING_ADDRESS))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_unknown_order_is_404(session, rng):
    with pytest.raises(NotFoundError):
        FulfilmentService(session, rng).get(uuid.uuid4())


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------
def test_order_confirmed_opens_a_fulfilment_and_starts_picking(database, rng):
    order_id = uuid.uuid4()

    with database.session() as session:
        handle_order_confirmed(
            order_confirmed(order_id), Topic.ORDER_CONFIRMED, session=session, rng=rng
        )

    with database.session() as session:
        fulfilment = FulfilmentService(session, rng).get(order_id)
        # Not left in PENDING with nothing scheduling it.
        assert fulfilment.status == FulfilmentStatus.PICKING.value
        assert fulfilment.customer_id == CUSTOMER_ID

    assert _outbox_topics(database) == [Topic.FULFILMENT_STARTED]


def test_redelivered_order_confirmed_does_not_create_a_second_shipment(database, rng):
    order_id = uuid.uuid4()
    event = order_confirmed(order_id)

    with database.session() as session:
        handle_order_confirmed(event, Topic.ORDER_CONFIRMED, session=session, rng=rng)

    with pytest.raises(DuplicateEventError), database.session() as session:
        handle_order_confirmed(event, Topic.ORDER_CONFIRMED, session=session, rng=rng)

    with database.session() as session:
        assert session.query(Fulfilment).count() == 1
        assert session.query(OutboxEvent).count() == 1


def test_fulfilment_and_its_event_commit_together(database, rng):
    order_id = uuid.uuid4()

    with pytest.raises(RuntimeError), database.session() as session:
        handle_order_confirmed(
            order_confirmed(order_id), Topic.ORDER_CONFIRMED, session=session, rng=rng
        )
        raise RuntimeError("failure after the handler")

    with database.session() as session:
        assert session.query(Fulfilment).count() == 0
        assert session.query(OutboxEvent).count() == 0


def test_event_without_a_shipping_address_fails_permanently(database, rng):
    """Nothing to fulfil, and no retry will produce an address."""
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_confirmed(
            EventEnvelope(
                event_type=EventType.ORDER_CONFIRMED,
                source="order-service",
                payload={"order_id": str(uuid.uuid4())},
            ),
            Topic.ORDER_CONFIRMED,
            session=session,
            rng=rng,
        )


def test_event_without_an_order_id_fails_permanently(database, rng):
    with pytest.raises(PermanentEventError), database.session() as session:
        handle_order_confirmed(
            EventEnvelope(
                event_type=EventType.ORDER_CONFIRMED,
                source="order-service",
                payload={"shipping_address": SHIPPING_ADDRESS},
            ),
            Topic.ORDER_CONFIRMED,
            session=session,
            rng=rng,
        )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_full_rest_flow_publishes_shipped_and_delivered(client, staff_headers, database, open_fulfilment):
    order_id = open_fulfilment["order_id"]

    advance(client, staff_headers, order_id, "pick", "pack", "ship", "deliver")

    body = client.get(f"/fulfilment/{order_id}", headers=staff_headers).json()
    assert body["status"] == "DELIVERED"
    assert Topic.ORDER_SHIPPED in _outbox_topics(database)
    assert Topic.ORDER_DELIVERED in _outbox_topics(database)


def test_ship_assigns_a_carrier_and_tracking_number(client, staff_headers, open_fulfilment):
    order_id = open_fulfilment["order_id"]

    body = advance(client, staff_headers, order_id, "pick", "pack", "ship")

    assert body["carrier"]
    assert body["tracking_number"]
    assert body["tracking_number"].startswith(body["carrier"][:3])


def test_ship_accepts_an_explicit_carrier(client, staff_headers, open_fulfilment):
    order_id = open_fulfilment["order_id"]
    advance(client, staff_headers, order_id, "pick", "pack")

    body = client.post(
        f"/fulfilment/{order_id}/ship", json={"carrier": "bluedart"}, headers=staff_headers
    ).json()

    assert body["carrier"] == "BLUEDART"


def test_illegal_rest_transition_returns_409(client, staff_headers, open_fulfilment):
    response = client.post(
        f"/fulfilment/{open_fulfilment['order_id']}/deliver", headers=staff_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["current_status"] == "PENDING"


def test_customer_cannot_drive_fulfilment(client, customer_headers, open_fulfilment):
    response = client.post(
        f"/fulfilment/{open_fulfilment['order_id']}/pick", headers=customer_headers
    )
    assert response.status_code == 403


def test_tracking_shows_an_estimated_delivery_once_shipped(client, staff_headers, customer_headers, open_fulfilment):
    order_id = open_fulfilment["order_id"]

    before = client.get(f"/fulfilment/{order_id}/tracking", headers=customer_headers).json()
    assert before["estimated_delivery"] is None

    advance(client, staff_headers, order_id, "pick", "pack", "ship")

    after = client.get(f"/fulfilment/{order_id}/tracking", headers=customer_headers).json()
    assert after["estimated_delivery"] is not None
    assert after["tracking_number"]


def test_customer_cannot_track_someone_elses_shipment(client, other_customer_headers, open_fulfilment):
    response = client.get(
        f"/fulfilment/{open_fulfilment['order_id']}/tracking", headers=other_customer_headers
    )
    assert response.status_code == 404


def test_delivery_failure_endpoint_records_the_attempt(client, staff_headers, open_fulfilment):
    order_id = open_fulfilment["order_id"]
    advance(client, staff_headers, order_id, "pick", "pack", "ship")

    body = client.post(
        f"/fulfilment/{order_id}/delivery-failed",
        json={"reason": "RECIPIENT_UNAVAILABLE"},
        headers=staff_headers,
    ).json()

    assert body["status"] == "FAILED_DELIVERY"
    assert body["delivery_attempts"] == 1
    assert body["failure_reason"] == "RECIPIENT_UNAVAILABLE"


def test_delivery_failure_publishes_no_order_event(client, staff_headers, database, open_fulfilment):
    """The order is still legitimately SHIPPED from the customer's view."""
    order_id = open_fulfilment["order_id"]
    advance(client, staff_headers, order_id, "pick", "pack", "ship")

    client.post(
        f"/fulfilment/{order_id}/delivery-failed",
        json={"reason": "NOBODY_HOME"},
        headers=staff_headers,
    )

    assert _outbox_topics(database).count(Topic.ORDER_DELIVERED) == 0


def test_create_endpoint_is_idempotent(client, staff_headers):
    payload = {"order_id": str(uuid.uuid4()), "shipping_address": SHIPPING_ADDRESS}

    first = client.post("/fulfilment", json=payload, headers=staff_headers).json()
    second = client.post("/fulfilment", json=payload, headers=staff_headers).json()

    assert first["fulfilment_id"] == second["fulfilment_id"]


def test_list_can_be_filtered_by_status(client, staff_headers, open_fulfilment):
    assert client.get("/fulfilment?status=PENDING", headers=staff_headers).json()["total"] == 1
    assert client.get("/fulfilment?status=SHIPPED", headers=staff_headers).json()["total"] == 0


def test_unknown_fulfilment_returns_404(client, staff_headers):
    assert client.get(f"/fulfilment/{uuid.uuid4()}", headers=staff_headers).status_code == 404


def test_fulfilment_requires_authentication(client, open_fulfilment):
    assert client.get(f"/fulfilment/{open_fulfilment['order_id']}").status_code == 401


def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "fulfilment-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/fulfilment/{order_id}/ship" in paths
    assert "/fulfilment/{order_id}/tracking" in paths
