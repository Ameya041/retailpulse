"""Order service API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.order_state import OrderStatus


class CartItemAdd(BaseModel):
    product_id: uuid.UUID
    quantity: Annotated[int, Field(gt=0, le=100)]


class CartItemUpdate(BaseModel):
    quantity: Annotated[int, Field(ge=0, le=100, description="0 removes the line.")]


class CartItemRead(BaseModel):
    cart_item_id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    is_orderable: bool


class CartRead(BaseModel):
    cart_id: uuid.UUID
    customer_id: uuid.UUID
    items: list[CartItemRead]
    item_count: int
    total_amount: Decimal
    currency: str
    updated_at: datetime


class OrderLineRequest(BaseModel):
    product_id: uuid.UUID
    quantity: Annotated[int, Field(gt=0, le=100)]


class OrderCreateRequest(BaseModel):
    """Create an order, either from explicit lines or from the customer's cart."""

    shipping_address: Annotated[str, Field(min_length=10, max_length=500)]
    lines: list[OrderLineRequest] | None = Field(
        default=None,
        description="Explicit lines. Omit to order everything in the cart.",
    )

    @model_validator(mode="after")
    def _no_duplicate_products(self) -> OrderCreateRequest:
        if self.lines:
            ids = [line.product_id for line in self.lines]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate product_id in lines; merge the quantities.")
        return self


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_item_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    sku: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal


class OrderStatusHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    actor: str
    reason: str | None
    created_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: uuid.UUID
    customer_id: uuid.UUID
    status: OrderStatus
    total_amount: Decimal
    currency: str
    shipping_address: str
    cancellation_reason: str | None
    items: list[OrderItemRead]
    created_at: datetime
    updated_at: datetime


class OrderDetailRead(OrderRead):
    """Order plus its transition history."""

    transitions: list[OrderStatusHistoryRead]
    allowed_next_statuses: list[OrderStatus]


class OrderStatusUpdateRequest(BaseModel):
    status: OrderStatus
    reason: str | None = Field(default=None, max_length=200)


class OrderCancelRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=120)] = "CUSTOMER_REQUESTED"
