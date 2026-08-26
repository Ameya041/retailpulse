"""Fulfilment service API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models import FulfilmentStatus


class FulfilmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fulfilment_id: uuid.UUID
    order_id: uuid.UUID
    customer_id: uuid.UUID | None
    status: FulfilmentStatus
    carrier: str | None
    tracking_number: str | None
    shipping_address: str
    delivery_attempts: int
    failure_reason: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FulfilmentCreateRequest(BaseModel):
    """Start fulfilment manually. The normal path is the ORDER_CONFIRMED event."""

    order_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    shipping_address: Annotated[str, Field(min_length=10, max_length=500)]


class ShipRequest(BaseModel):
    carrier: Annotated[str, Field(min_length=2, max_length=24)] | None = None


class DeliveryFailureRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=120)] = "RECIPIENT_UNAVAILABLE"


class TrackingRead(BaseModel):
    """Customer-facing tracking view."""

    order_id: uuid.UUID
    status: FulfilmentStatus
    carrier: str | None
    tracking_number: str | None
    delivery_attempts: int
    shipped_at: datetime | None
    delivered_at: datetime | None
    estimated_delivery: datetime | None
