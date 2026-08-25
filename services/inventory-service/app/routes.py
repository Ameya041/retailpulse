"""Inventory HTTP routes.

Authorization:

* Reads are open to any authenticated user (the storefront shows stock).
* Reserve/release/commit are service-to-service operations driven by the order
  saga, restricted to ADMIN and WAREHOUSE_OPERATOR.
* Restock and adjustments are warehouse operations.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import current_user, get_db_session, require_roles
from app.schemas import (
    AllocationRead,
    CommitRequest,
    InventoryRead,
    LocationCreate,
    LocationRead,
    LowStockRead,
    ProductInventorySummary,
    ReleaseRequest,
    ReleaseResponse,
    ReserveRequest,
    ReserveResponse,
    RestockRequest,
    StockMovementRead,
)
from app.service import InventoryService
from retailpulse_common.auth import Role, TokenPayload

router = APIRouter(prefix="/inventory", tags=["inventory"])
location_router = APIRouter(prefix="/locations", tags=["locations"])

SessionDep = Annotated[Session, Depends(get_db_session)]
AuthedDep = Annotated[TokenPayload, Depends(current_user)]
OperatorDep = Annotated[
    TokenPayload, Depends(require_roles(Role.ADMIN, Role.WAREHOUSE_OPERATOR))
]

AUTH_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Authenticated but the role is not permitted."},
}


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
@location_router.post(
    "",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a store or warehouse",
    responses={**AUTH_ERRORS, 409: {"description": "Location code already exists."}},
)
def create_location(
    payload: LocationCreate, session: SessionDep, _: OperatorDep
) -> LocationRead:
    """Create a stock-holding location. Requires ADMIN or WAREHOUSE_OPERATOR."""
    location = InventoryService(session).create_location(
        payload.code, payload.name, payload.city
    )
    return LocationRead.model_validate(location)


@location_router.get("", response_model=list[LocationRead], summary="List locations")
def list_locations(
    session: SessionDep,
    include_inactive: Annotated[bool, Query()] = False,
) -> list[LocationRead]:
    """All stock-holding locations. Public so the storefront can show pickup options."""
    locations = InventoryService(session).list_locations(active_only=not include_inactive)
    return [LocationRead.model_validate(loc) for loc in locations]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@router.get(
    "/low-stock",
    response_model=list[LowStockRead],
    summary="Products at or below their reorder threshold",
    responses=AUTH_ERRORS,
)
def low_stock(
    session: SessionDep,
    _: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[LowStockRead]:
    """Replenishment worklist. Feeds the ML-driven reorder recommendations."""
    return [
        LowStockRead(
            product_id=item.product_id,
            location_id=item.location_id,
            location_code=item.location.code,
            available_quantity=item.available_quantity,
            reorder_threshold=item.reorder_threshold,
            shortfall=max(0, item.reorder_threshold - item.available_quantity),
        )
        for item in InventoryService(session).low_stock(limit)
    ]


@router.get(
    "/{product_id}",
    response_model=ProductInventorySummary,
    summary="Network-wide stock for a product",
)
def get_inventory(product_id: uuid.UUID, session: SessionDep) -> ProductInventorySummary:
    """Aggregate view across every location holding this product."""
    items = InventoryService(session).get_product_inventory(product_id)
    reads = [InventoryRead.from_model(item) for item in items]
    return ProductInventorySummary(
        product_id=product_id,
        total_available=sum(r.available_quantity for r in reads),
        total_reserved=sum(r.reserved_quantity for r in reads),
        locations_in_stock=sum(1 for r in reads if r.available_quantity > 0),
        is_low_anywhere=any(r.is_low for r in reads),
        locations=reads,
    )


@router.get(
    "/{product_id}/locations",
    response_model=list[InventoryRead],
    summary="Per-location breakdown for a product",
)
def get_inventory_by_location(
    product_id: uuid.UUID, session: SessionDep
) -> list[InventoryRead]:
    """Stock at each location, highest availability first."""
    return [
        InventoryRead.from_model(item)
        for item in InventoryService(session).get_product_inventory(product_id)
    ]


@router.get(
    "/{product_id}/movements",
    response_model=list[StockMovementRead],
    summary="Stock movement history",
    responses=AUTH_ERRORS,
)
def get_movements(
    product_id: uuid.UUID,
    session: SessionDep,
    _: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[StockMovementRead]:
    """Append-only audit trail explaining how current quantities were reached."""
    return [
        StockMovementRead.model_validate(m)
        for m in InventoryService(session).movements(product_id, limit)
    ]


# ---------------------------------------------------------------------------
# Reservation lifecycle
# ---------------------------------------------------------------------------
@router.post(
    "/reserve",
    response_model=ReserveResponse,
    summary="Reserve stock for an order (transaction-safe)",
    responses={
        **AUTH_ERRORS,
        404: {"description": "Product has no inventory records."},
        409: {"description": "Insufficient stock; nothing was reserved."},
    },
)
def reserve(
    payload: ReserveRequest, session: SessionDep, _: OperatorDep
) -> ReserveResponse:
    """Atomically move units from available to reserved.

    All lines succeed or none do. Rows are locked `FOR UPDATE` in a
    deterministic order, so concurrent reservations serialise instead of
    overselling, and cannot deadlock.

    Idempotent: re-reserving an order that already holds stock returns the
    existing allocations without holding more.
    """
    allocations, replay = InventoryService(session).reserve(payload)
    return ReserveResponse(
        order_id=payload.order_id,
        status="RESERVED",
        allocations=[
            AllocationRead(
                reservation_id=a.reservation_id,
                product_id=a.product_id,
                location_id=a.location_id,
                location_code=a.location_code,
                quantity=a.quantity,
            )
            for a in allocations
        ],
        idempotent_replay=replay,
    )


@router.post(
    "/release",
    response_model=ReleaseResponse,
    summary="Release held stock back to available",
    responses={**AUTH_ERRORS, 404: {"description": "No reservation for that order."}},
)
def release(
    payload: ReleaseRequest, session: SessionDep, _: OperatorDep
) -> ReleaseResponse:
    """Compensating action for payment failure or cancellation.

    Idempotent: releasing an already-released order is a no-op, never a
    double credit.
    """
    lines, units, replay = InventoryService(session).release(payload)
    return ReleaseResponse(
        order_id=payload.order_id,
        released_lines=lines,
        released_units=units,
        idempotent_replay=replay,
    )


@router.post(
    "/commit",
    response_model=ReleaseResponse,
    summary="Consume reserved stock once the order ships",
    responses={**AUTH_ERRORS, 404: {"description": "No held reservation for that order."}},
)
def commit(payload: CommitRequest, session: SessionDep, _: OperatorDep) -> ReleaseResponse:
    """Reserved units permanently leave the location. `available` is untouched."""
    lines, units, replay = InventoryService(session).commit(payload.order_id)
    return ReleaseResponse(
        order_id=payload.order_id,
        released_lines=lines,
        released_units=units,
        idempotent_replay=replay,
    )


@router.post(
    "/restock",
    response_model=InventoryRead,
    summary="Add stock at a location",
    responses={**AUTH_ERRORS, 404: {"description": "Unknown location."}},
)
def restock(payload: RestockRequest, session: SessionDep, _: OperatorDep) -> InventoryRead:
    """Receive a delivery. Creates the (product, location) row on first receipt."""
    return InventoryRead.from_model(InventoryService(session).restock(payload))
