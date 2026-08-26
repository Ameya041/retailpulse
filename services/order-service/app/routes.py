"""Cart and order routes."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import current_user, get_catalog, get_db_session, require_roles
from app.order_state import OrderStatus, next_states
from app.product_client import ProductCatalog
from app.schemas import (
    CartItemAdd,
    CartItemRead,
    CartItemUpdate,
    CartRead,
    OrderCancelRequest,
    OrderCreateRequest,
    OrderDetailRead,
    OrderRead,
    OrderStatusHistoryRead,
    OrderStatusUpdateRequest,
)
from app.service import CartService, OrderService
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.pagination import Page, PageParams, page_params

cart_router = APIRouter(prefix="/cart", tags=["cart"])
order_router = APIRouter(prefix="/orders", tags=["orders"])

SessionDep = Annotated[Session, Depends(get_db_session)]
CatalogDep = Annotated[ProductCatalog, Depends(get_catalog)]
AuthedDep = Annotated[TokenPayload, Depends(current_user)]
PageDep = Annotated[PageParams, Depends(page_params)]
StaffDep = Annotated[
    TokenPayload, Depends(require_roles(Role.ADMIN, Role.WAREHOUSE_OPERATOR))
]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]

AUTH_ERRORS = {401: {"description": "Missing or invalid bearer token."}}


def _cart_read(cart, products) -> CartRead:
    items: list[CartItemRead] = []
    total = Decimal("0.00")
    currency = "INR"
    for item in cart.items:
        product = products.get(item.product_id)
        unit_price = product.price if product else Decimal("0.00")
        subtotal = (unit_price * item.quantity).quantize(Decimal("0.01"))
        total += subtotal
        if product:
            currency = product.currency
        items.append(
            CartItemRead(
                cart_item_id=item.cart_item_id,
                product_id=item.product_id,
                sku=product.sku if product else "UNKNOWN",
                product_name=product.name if product else "Unavailable product",
                quantity=item.quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                # Surfaced so the UI can flag a line that went out of catalog
                # while it sat in the basket.
                is_orderable=bool(product and product.is_orderable),
            )
        )
    return CartRead(
        cart_id=cart.cart_id,
        customer_id=cart.customer_id,
        items=items,
        item_count=sum(i.quantity for i in cart.items),
        total_amount=total,
        currency=currency,
        updated_at=cart.updated_at,
    )


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------
@cart_router.get("", response_model=CartRead, summary="Get my cart", responses=AUTH_ERRORS)
def get_cart(user: AuthedDep, session: SessionDep, catalog: CatalogDep) -> CartRead:
    """The caller's cart, priced live from the catalog.

    Cart prices are always current; they are only frozen when an order is
    placed.
    """
    cart, products = CartService(session, catalog).get_cart(user.user_id)
    return _cart_read(cart, products)


@cart_router.post(
    "/items",
    response_model=CartRead,
    summary="Add a product to my cart",
    responses={**AUTH_ERRORS, 404: {"description": "No such product."}, 409: {"description": "Product unavailable."}},
)
def add_item(
    payload: CartItemAdd, user: AuthedDep, session: SessionDep, catalog: CatalogDep
) -> CartRead:
    """Adding a product already in the cart increases its quantity."""
    service = CartService(session, catalog)
    service.add_item(user.user_id, payload.product_id, payload.quantity)
    cart, products = service.get_cart(user.user_id)
    return _cart_read(cart, products)


@cart_router.put(
    "/items/{product_id}",
    response_model=CartRead,
    summary="Set the quantity of a cart line",
    responses={**AUTH_ERRORS, 404: {"description": "Product not in cart."}},
)
def update_item(
    product_id: uuid.UUID,
    payload: CartItemUpdate,
    user: AuthedDep,
    session: SessionDep,
    catalog: CatalogDep,
) -> CartRead:
    """Setting the quantity to 0 removes the line."""
    service = CartService(session, catalog)
    service.update_item(user.user_id, product_id, payload.quantity)
    cart, products = service.get_cart(user.user_id)
    return _cart_read(cart, products)


@cart_router.delete("", response_model=CartRead, summary="Empty my cart", responses=AUTH_ERRORS)
def clear_cart(user: AuthedDep, session: SessionDep, catalog: CatalogDep) -> CartRead:
    """Remove every line from the cart."""
    service = CartService(session, catalog)
    service.clear(user.user_id)
    cart, products = service.get_cart(user.user_id)
    return _cart_read(cart, products)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@order_router.post(
    "",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Place an order",
    responses={
        **AUTH_ERRORS,
        400: {"description": "Empty cart, unknown product, or mixed currencies."},
        409: {"description": "A product is no longer available."},
        503: {"description": "The product catalog is unreachable."},
    },
)
def create_order(
    payload: OrderCreateRequest,
    user: AuthedDep,
    session: SessionDep,
    catalog: CatalogDep,
) -> OrderRead:
    """Create an order from explicit lines, or from the caller's cart.

    Prices are snapshotted at this moment, so a later catalog change cannot
    alter the agreed total. The order starts in CREATED; inventory reservation
    and payment happen asynchronously from here.
    """
    order = OrderService(session, catalog).create(user.user_id, payload)
    return OrderRead.model_validate(order)


@order_router.get(
    "",
    response_model=Page[OrderRead],
    summary="List my orders",
    responses=AUTH_ERRORS,
)
def list_my_orders(
    user: AuthedDep, session: SessionDep, catalog: CatalogDep, params: PageDep
) -> Page[OrderRead]:
    """A customer sees only their own orders. Enforced server-side."""
    orders, total = OrderService(session, catalog).list_for_customer(
        user.user_id, offset=params.offset, limit=params.limit
    )
    return Page.build([OrderRead.model_validate(o) for o in orders], total, params)


@order_router.get(
    "/all",
    response_model=Page[OrderRead],
    summary="List all orders (staff)",
    responses={**AUTH_ERRORS, 403: {"description": "Requires ADMIN or WAREHOUSE_OPERATOR."}},
)
def list_all_orders(
    session: SessionDep,
    catalog: CatalogDep,
    params: PageDep,
    _: StaffDep,
    status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
) -> Page[OrderRead]:
    """Operational view across every customer."""
    orders, total = OrderService(session, catalog).list_all(
        status=status_filter, offset=params.offset, limit=params.limit
    )
    return Page.build([OrderRead.model_validate(o) for o in orders], total, params)


@order_router.get(
    "/{order_id}",
    response_model=OrderDetailRead,
    summary="Get an order",
    responses={**AUTH_ERRORS, 404: {"description": "No such order, or not yours."}},
)
def get_order(
    order_id: uuid.UUID, user: AuthedDep, session: SessionDep, catalog: CatalogDep
) -> OrderDetailRead:
    """Order detail with its full transition history.

    Requesting someone else's order returns 404, not 403 -- confirming an
    order ID exists would itself leak information.
    """
    service = OrderService(session, catalog)
    order = (
        service.get(order_id)
        if user.role in (Role.ADMIN, Role.WAREHOUSE_OPERATOR)
        else service.get_for_customer(order_id, user.user_id)
    )
    # `allowed_next_statuses` is derived from the state machine, not stored on
    # the row, so the detail model is composed rather than validated straight
    # off the ORM object.
    return OrderDetailRead(
        **OrderRead.model_validate(order).model_dump(),
        transitions=[OrderStatusHistoryRead.model_validate(t) for t in order.transitions],
        allowed_next_statuses=next_states(OrderStatus(order.status)),
    )


@order_router.post(
    "/{order_id}/cancel",
    response_model=OrderRead,
    summary="Cancel my order",
    responses={
        **AUTH_ERRORS,
        403: {"description": "Already in fulfilment; cancellation no longer allowed."},
        404: {"description": "No such order, or not yours."},
        409: {"description": "Order already in a terminal state."},
    },
)
def cancel_order(
    order_id: uuid.UUID,
    payload: OrderCancelRequest,
    user: AuthedDep,
    session: SessionDep,
    catalog: CatalogDep,
) -> OrderRead:
    """Customer-initiated cancellation, permitted only before fulfilment starts."""
    order = OrderService(session, catalog).cancel_as_customer(
        order_id, user.user_id, payload.reason
    )
    return OrderRead.model_validate(order)


@order_router.patch(
    "/{order_id}/status",
    response_model=OrderRead,
    summary="Move an order to a new status (staff)",
    responses={
        **AUTH_ERRORS,
        403: {"description": "Requires ADMIN or WAREHOUSE_OPERATOR."},
        404: {"description": "No such order."},
        409: {"description": "Illegal state transition."},
    },
)
def update_status(
    order_id: uuid.UUID,
    payload: OrderStatusUpdateRequest,
    session: SessionDep,
    catalog: CatalogDep,
    staff: StaffDep,
) -> OrderRead:
    """Drive an order through the state machine.

    Illegal transitions (DELIVERED back to CREATED, say) are rejected with 409
    and the response lists which statuses *are* reachable from here.
    """
    order = OrderService(session, catalog).transition(
        order_id, payload.status, actor=staff.role.value.lower(), reason=payload.reason
    )
    return OrderRead.model_validate(order)
