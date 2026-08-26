"""Fulfilment routes.

Warehouse staff drive the shipment forward here; each transition stages an
event that the order service consumes to advance the order.
"""

from __future__ import annotations

import random
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import current_user, get_db_session, get_rng, require_roles
from app.handlers import SERVICE
from app.models import FulfilmentStatus
from app.schemas import (
    DeliveryFailureRequest,
    FulfilmentCreateRequest,
    FulfilmentRead,
    ShipRequest,
    TrackingRead,
)
from app.service import FulfilmentService
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.errors import NotFoundError
from retailpulse_common.events.envelope import EventEnvelope
from retailpulse_common.events.outbox import enqueue
from retailpulse_common.events.producer import order_key
from retailpulse_common.events.topics import EventType, Topic
from retailpulse_common.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/fulfilment", tags=["fulfilment"])

SessionDep = Annotated[Session, Depends(get_db_session)]
RngDep = Annotated[random.Random, Depends(get_rng)]
AuthedDep = Annotated[TokenPayload, Depends(current_user)]
PageDep = Annotated[PageParams, Depends(page_params)]
StaffDep = Annotated[
    TokenPayload, Depends(require_roles(Role.ADMIN, Role.WAREHOUSE_OPERATOR))
]

AUTH_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Requires ADMIN or WAREHOUSE_OPERATOR."},
}


def _publish(session: Session, topic: str, event_type: str, order_id: uuid.UUID, **payload) -> None:
    """Stage an event on the caller's transaction.

    Same reasoning as everywhere else: the status change and the event
    announcing it commit together, or a shipment moves that nobody hears about.
    """
    enqueue(
        session,
        topic,
        EventEnvelope(
            event_type=event_type,
            source=SERVICE,
            payload={"order_id": str(order_id), **payload},
        ),
        key=order_key(order_id),
    )


@router.get(
    "",
    response_model=Page[FulfilmentRead],
    summary="List fulfilments (staff)",
    responses=AUTH_ERRORS,
)
def list_fulfilments(
    session: SessionDep,
    rng: RngDep,
    params: PageDep,
    _: StaffDep,
    status_filter: Annotated[FulfilmentStatus | None, Query(alias="status")] = None,
) -> Page[FulfilmentRead]:
    """Warehouse worklist, filterable by status."""
    rows, total = FulfilmentService(session, rng).list_fulfilments(
        status=status_filter, offset=params.offset, limit=params.limit
    )
    return Page.build([FulfilmentRead.model_validate(f) for f in rows], total, params)


@router.post(
    "",
    response_model=FulfilmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Open a fulfilment manually",
    responses=AUTH_ERRORS,
)
def create_fulfilment(
    payload: FulfilmentCreateRequest, session: SessionDep, rng: RngDep, _: StaffDep
) -> FulfilmentRead:
    """The normal path is the ORDER_CONFIRMED event; this is the manual override.

    Idempotent: an order that already has a fulfilment returns it unchanged.
    """
    fulfilment = FulfilmentService(session, rng).start(
        payload.order_id, payload.shipping_address, customer_id=payload.customer_id
    )
    return FulfilmentRead.model_validate(fulfilment)


@router.get(
    "/{order_id}",
    response_model=FulfilmentRead,
    summary="Get the fulfilment for an order",
    responses={**AUTH_ERRORS, 404: {"description": "No fulfilment for that order."}},
)
def get_fulfilment(
    order_id: uuid.UUID, session: SessionDep, rng: RngDep, user: AuthedDep
) -> FulfilmentRead:
    """Customers may only see their own; staff may see any."""
    fulfilment = FulfilmentService(session, rng).get(order_id)

    if user.role is Role.CUSTOMER and fulfilment.customer_id != user.user_id:
        raise NotFoundError(
            f"No fulfilment exists for order {order_id}.",
            details={"order_id": str(order_id)},
        )

    return FulfilmentRead.model_validate(fulfilment)


@router.get(
    "/{order_id}/tracking",
    response_model=TrackingRead,
    summary="Track a shipment",
    responses={**AUTH_ERRORS, 404: {"description": "No fulfilment for that order."}},
)
def track(
    order_id: uuid.UUID, session: SessionDep, rng: RngDep, user: AuthedDep
) -> TrackingRead:
    """Customer-facing tracking, including an estimated delivery date."""
    service = FulfilmentService(session, rng)
    fulfilment = service.get(order_id)

    if user.role is Role.CUSTOMER and fulfilment.customer_id != user.user_id:
        raise NotFoundError(
            f"No fulfilment exists for order {order_id}.",
            details={"order_id": str(order_id)},
        )

    return TrackingRead(
        order_id=fulfilment.order_id,
        status=FulfilmentStatus(fulfilment.status),
        carrier=fulfilment.carrier,
        tracking_number=fulfilment.tracking_number,
        delivery_attempts=fulfilment.delivery_attempts,
        shipped_at=fulfilment.shipped_at,
        delivered_at=fulfilment.delivered_at,
        estimated_delivery=service.estimated_delivery(fulfilment),
    )


@router.post(
    "/{order_id}/pick",
    response_model=FulfilmentRead,
    summary="Start picking a shipment",
    responses={**AUTH_ERRORS, 404: {"description": "No such fulfilment."}, 409: {"description": "Illegal transition."}},
)
def pick(order_id: uuid.UUID, session: SessionDep, rng: RngDep, _: StaffDep) -> FulfilmentRead:
    """Move PENDING to PICKING. The ORDER_CONFIRMED handler does this
    automatically; this endpoint covers manually-created fulfilments."""
    return FulfilmentRead.model_validate(FulfilmentService(session, rng).begin_picking(order_id))


@router.post(
    "/{order_id}/pack",
    response_model=FulfilmentRead,
    summary="Mark a shipment packed",
    responses={**AUTH_ERRORS, 404: {"description": "No such fulfilment."}, 409: {"description": "Illegal transition."}},
)
def pack(order_id: uuid.UUID, session: SessionDep, rng: RngDep, _: StaffDep) -> FulfilmentRead:
    """Picking complete, parcel ready for a carrier."""
    return FulfilmentRead.model_validate(FulfilmentService(session, rng).mark_packed(order_id))


@router.post(
    "/{order_id}/ship",
    response_model=FulfilmentRead,
    summary="Dispatch a shipment",
    responses={**AUTH_ERRORS, 404: {"description": "No such fulfilment."}, 409: {"description": "Illegal transition."}},
)
def ship(
    order_id: uuid.UUID,
    payload: ShipRequest,
    session: SessionDep,
    rng: RngDep,
    _: StaffDep,
) -> FulfilmentRead:
    """Hand the parcel to a carrier and assign tracking.

    Publishes ORDER_SHIPPED, which advances the order and lets the inventory
    service consume the reservation permanently.
    """
    service = FulfilmentService(session, rng)
    fulfilment = service.ship(order_id, carrier=payload.carrier)

    _publish(
        session,
        Topic.ORDER_SHIPPED,
        EventType.ORDER_SHIPPED,
        order_id,
        carrier=fulfilment.carrier,
        tracking_number=fulfilment.tracking_number,
    )
    return FulfilmentRead.model_validate(fulfilment)


@router.post(
    "/{order_id}/deliver",
    response_model=FulfilmentRead,
    summary="Confirm delivery",
    responses={**AUTH_ERRORS, 404: {"description": "No such fulfilment."}, 409: {"description": "Illegal transition."}},
)
def deliver(
    order_id: uuid.UUID, session: SessionDep, rng: RngDep, _: StaffDep
) -> FulfilmentRead:
    """Final step. Publishes ORDER_DELIVERED, closing the order."""
    fulfilment = FulfilmentService(session, rng).deliver(order_id)

    _publish(
        session,
        Topic.ORDER_DELIVERED,
        EventType.ORDER_DELIVERED,
        order_id,
        delivered_at=fulfilment.delivered_at.isoformat() if fulfilment.delivered_at else None,
    )
    return FulfilmentRead.model_validate(fulfilment)


@router.post(
    "/{order_id}/delivery-failed",
    response_model=FulfilmentRead,
    summary="Record a failed delivery attempt",
    responses={**AUTH_ERRORS, 404: {"description": "No such fulfilment."}, 409: {"description": "Illegal transition."}},
)
def delivery_failed(
    order_id: uuid.UUID,
    payload: DeliveryFailureRequest,
    session: SessionDep,
    rng: RngDep,
    _: StaffDep,
) -> FulfilmentRead:
    """Nobody home. The parcel can be re-shipped up to the attempt limit.

    No event is published: the order is still legitimately SHIPPED from the
    customer's point of view, and a failed attempt is a fulfilment-internal
    detail rather than an order lifecycle change.
    """
    return FulfilmentRead.model_validate(
        FulfilmentService(session, rng).fail_delivery(order_id, payload.reason)
    )
