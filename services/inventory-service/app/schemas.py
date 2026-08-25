"""Inventory API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocationCreate(BaseModel):
    code: Annotated[str, Field(pattern=r"^[A-Z]{2,8}[0-9]{0,4}$", max_length=16)]
    name: Annotated[str, Field(min_length=2, max_length=120)]
    city: Annotated[str, Field(min_length=2, max_length=80)]


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    location_id: uuid.UUID
    code: str
    name: str
    city: str
    is_active: bool


class InventoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    inventory_id: uuid.UUID
    product_id: uuid.UUID
    location_id: uuid.UUID
    location_code: str
    location_name: str
    available_quantity: int
    reserved_quantity: int
    total_quantity: int
    reorder_threshold: int
    is_low: bool
    updated_at: datetime

    @classmethod
    def from_model(cls, item) -> InventoryRead:
        return cls(
            inventory_id=item.inventory_id,
            product_id=item.product_id,
            location_id=item.location_id,
            location_code=item.location.code,
            location_name=item.location.name,
            available_quantity=item.available_quantity,
            reserved_quantity=item.reserved_quantity,
            total_quantity=item.total_quantity,
            reorder_threshold=item.reorder_threshold,
            is_low=item.is_low,
            updated_at=item.updated_at,
        )


class ProductInventorySummary(BaseModel):
    """Network-wide view of one product."""

    product_id: uuid.UUID
    total_available: int
    total_reserved: int
    locations_in_stock: int
    is_low_anywhere: bool
    locations: list[InventoryRead]


class ReservationLine(BaseModel):
    product_id: uuid.UUID
    quantity: Annotated[int, Field(gt=0, le=10_000)]
    # Optional: when absent the service allocates across locations itself.
    location_id: uuid.UUID | None = None


class ReserveRequest(BaseModel):
    """Reserve stock for an order.

    All lines succeed or none do. A partially reserved order is worse than a
    rejected one -- the customer would be charged for items that were never
    held, and the compensating cleanup is a source of bugs.
    """

    order_id: uuid.UUID
    lines: Annotated[list[ReservationLine], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_duplicate_lines(self) -> ReserveRequest:
        seen = {(line.product_id, line.location_id) for line in self.lines}
        if len(seen) != len(self.lines):
            raise ValueError(
                "Duplicate (product_id, location_id) lines; merge the quantities instead."
            )
        return self


class AllocationRead(BaseModel):
    """Where a reservation actually took units from."""

    reservation_id: uuid.UUID
    product_id: uuid.UUID
    location_id: uuid.UUID
    location_code: str
    quantity: int


class ReserveResponse(BaseModel):
    order_id: uuid.UUID
    status: str
    allocations: list[AllocationRead]
    idempotent_replay: bool = Field(
        default=False,
        description="True when this order was already reserved and no new stock was held.",
    )


class ReleaseRequest(BaseModel):
    """Return held stock to available.

    Used when payment fails or the customer cancels before fulfilment.
    """

    order_id: uuid.UUID
    reason: Annotated[str, Field(max_length=200)] = "ORDER_CANCELLED"


class CommitRequest(BaseModel):
    """Permanently remove reserved stock once the order ships."""

    order_id: uuid.UUID


class ReleaseResponse(BaseModel):
    order_id: uuid.UUID
    released_lines: int
    released_units: int
    idempotent_replay: bool = False


class RestockRequest(BaseModel):
    product_id: uuid.UUID
    location_id: uuid.UUID
    quantity: Annotated[int, Field(gt=0, le=1_000_000)]
    reorder_threshold: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=200)


class StockMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    movement_id: uuid.UUID
    product_id: uuid.UUID
    location_id: uuid.UUID
    movement_type: str
    quantity_delta: int
    available_after: int
    reserved_after: int
    reference_id: uuid.UUID | None
    note: str | None
    created_at: datetime


class LowStockRead(BaseModel):
    product_id: uuid.UUID
    location_id: uuid.UUID
    location_code: str
    available_quantity: int
    reorder_threshold: int
    shortfall: int
