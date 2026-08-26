"""Payment routes.

The normal payment path is event-driven; these endpoints exist for customers
to view their payment, and for staff to reconcile and refund.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import current_user, get_db_session, get_gateway, require_roles
from app.gateway import PaymentGateway
from app.models import PaymentStatus
from app.schemas import ChargeRequest, PaymentRead, PaymentStatsRead, RefundRequest
from app.service import PaymentService
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/payments", tags=["payments"])

SessionDep = Annotated[Session, Depends(get_db_session)]
GatewayDep = Annotated[PaymentGateway, Depends(get_gateway)]
AuthedDep = Annotated[TokenPayload, Depends(current_user)]
PageDep = Annotated[PageParams, Depends(page_params)]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]

AUTH_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Authenticated but the role is not permitted."},
}


@router.get(
    "/stats",
    response_model=PaymentStatsRead,
    summary="Payment totals and approval rate",
    responses=AUTH_ERRORS,
)
def stats(session: SessionDep, gateway: GatewayDep, _: AdminDep) -> PaymentStatsRead:
    """Aggregates for the admin dashboard. Requires ADMIN."""
    return PaymentStatsRead(**PaymentService(session, gateway).stats())


@router.get(
    "",
    response_model=Page[PaymentRead],
    summary="List payments (staff)",
    responses=AUTH_ERRORS,
)
def list_payments(
    session: SessionDep,
    gateway: GatewayDep,
    params: PageDep,
    _: AdminDep,
    status_filter: Annotated[PaymentStatus | None, Query(alias="status")] = None,
) -> Page[PaymentRead]:
    """Reconciliation view across every customer. Requires ADMIN."""
    payments, total = PaymentService(session, gateway).list_payments(
        status=status_filter, offset=params.offset, limit=params.limit
    )
    return Page.build([PaymentRead.model_validate(p) for p in payments], total, params)


@router.post(
    "",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Charge for an order (manual)",
    responses={**AUTH_ERRORS, 400: {"description": "Invalid amount."}},
)
def charge(
    payload: ChargeRequest, session: SessionDep, gateway: GatewayDep, _: AdminDep
) -> PaymentRead:
    """Take payment directly rather than via the saga. Requires ADMIN.

    Idempotent: charging an order that already has a payment returns the
    existing one rather than charging again.
    """
    payment = PaymentService(session, gateway).charge(
        order_id=payload.order_id,
        amount=payload.amount,
        currency=payload.currency,
        customer_id=payload.customer_id,
        payment_method=payload.payment_method,
    )
    return PaymentRead.model_validate(payment)


@router.get(
    "/reference/{reference}",
    response_model=PaymentRead,
    summary="Look up a payment by transaction reference",
    responses={**AUTH_ERRORS, 404: {"description": "No payment with that reference."}},
)
def get_by_reference(
    reference: str, session: SessionDep, gateway: GatewayDep, _: AdminDep
) -> PaymentRead:
    """Support lookup: the reference is what a customer quotes on the phone."""
    return PaymentRead.model_validate(PaymentService(session, gateway).get_by_reference(reference))


@router.get(
    "/{order_id}",
    response_model=PaymentRead,
    summary="Get the payment for an order",
    responses={**AUTH_ERRORS, 404: {"description": "No payment for that order."}},
)
def get_payment(
    order_id: uuid.UUID, session: SessionDep, gateway: GatewayDep, user: AuthedDep
) -> PaymentRead:
    """Customers may only see their own payment; staff may see any.

    A payment whose customer_id is unknown (created outside the saga) is
    treated as staff-only rather than public.
    """
    payment = PaymentService(session, gateway).get_by_order(order_id)

    if user.role is not Role.ADMIN and payment.customer_id != user.user_id:
        from retailpulse_common.errors import NotFoundError

        # 404 not 403: confirming the payment exists would leak that the order
        # exists and has been paid.
        raise NotFoundError(
            f"No payment exists for order {order_id}.",
            details={"order_id": str(order_id)},
        )

    return PaymentRead.model_validate(payment)


@router.post(
    "/{order_id}/refund",
    response_model=PaymentRead,
    summary="Refund a payment",
    responses={
        **AUTH_ERRORS,
        400: {"description": "Refund exceeds the amount charged."},
        404: {"description": "No payment for that order."},
        409: {"description": "Payment is not in a refundable state."},
    },
)
def refund(
    order_id: uuid.UUID,
    payload: RefundRequest,
    session: SessionDep,
    gateway: GatewayDep,
    _: AdminDep,
) -> PaymentRead:
    """Refund in full, or partially by supplying an amount. Requires ADMIN.

    Only a SUCCESS payment can be refunded; a failed or already-refunded
    payment returns 409.
    """
    payment = PaymentService(session, gateway).refund(
        order_id, amount=payload.amount, reason=payload.reason
    )
    return PaymentRead.model_validate(payment)
