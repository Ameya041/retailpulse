"""Payment service API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models import PaymentStatus


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: uuid.UUID
    order_id: uuid.UUID
    customer_id: uuid.UUID | None
    amount: Decimal
    currency: str
    status: PaymentStatus
    payment_method: str
    transaction_reference: str
    failure_reason: str | None
    refunded_amount: Decimal | None
    created_at: datetime
    updated_at: datetime


class ChargeRequest(BaseModel):
    """Manual charge, used by tests and the admin console.

    The normal path is driven by the PAYMENT_REQUESTED event, not this
    endpoint.
    """

    order_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    amount: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    payment_method: Annotated[str, Field(max_length=32)] = "CARD"


class RefundRequest(BaseModel):
    """Refund a successful payment.

    Amount is optional; omitting it refunds the full charge. Partial refunds
    are supported because a partially-returned order is a real scenario.
    """

    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    reason: Annotated[str, Field(min_length=3, max_length=120)] = "CUSTOMER_REQUESTED"


class PaymentStatsRead(BaseModel):
    """Aggregate view for the admin dashboard."""

    total_payments: int
    successful: int
    failed: int
    refunded: int
    pending: int
    success_rate: float
    total_collected: Decimal
    total_refunded: Decimal
    currency: str
