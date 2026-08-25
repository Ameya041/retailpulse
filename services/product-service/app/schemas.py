"""Request/response contracts for the product service.

These Pydantic models are the API contract and are intentionally separate from
the SQLAlchemy models. Coupling them would mean any column rename becomes a
breaking API change, and it makes it far too easy to leak internal fields.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ProductStatus

# Accepts either case; the validator normalises to uppercase. Pattern checks run
# before field validators in Pydantic, so a strictly-uppercase pattern would
# reject lowercase input outright instead of normalising it.
SKU_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9\-]{2,63}$"


class CategoryCreate(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=100)]
    description: str | None = Field(default=None, max_length=2000)


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    created_at: datetime


class ProductBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=5000)
    brand: str | None = Field(default=None, max_length=120)
    price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    weight_grams: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()


class ProductCreate(ProductBase):
    sku: Annotated[str, Field(pattern=SKU_PATTERN, description="Uppercase business key.")]
    category: Annotated[str, Field(min_length=2, max_length=100)] = Field(
        description="Category name. Created on first use."
    )
    status: ProductStatus = ProductStatus.ACTIVE

    @field_validator("sku")
    @classmethod
    def _upper_sku(cls, value: str) -> str:
        return value.upper()


class ProductUpdate(BaseModel):
    """Every field optional -- PUT here performs a partial update.

    SKU is deliberately absent: it is the business key other services and
    printed labels reference, so it is immutable after creation.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    category: str | None = Field(default=None, min_length=2, max_length=100)
    brand: str | None = Field(default=None, max_length=120)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    weight_grams: int | None = Field(default=None, ge=0, le=1_000_000)
    status: ProductStatus | None = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    sku: str
    name: str
    description: str | None
    category: str
    brand: str | None
    price: Decimal
    currency: str
    weight_grams: int | None
    status: ProductStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, product) -> ProductRead:
        """Flatten the category relationship into a plain name."""
        return cls(
            product_id=product.product_id,
            sku=product.sku,
            name=product.name,
            description=product.description,
            category=product.category.name,
            brand=product.brand,
            price=product.price,
            currency=product.currency,
            weight_grams=product.weight_grams,
            status=ProductStatus(product.status),
            created_at=product.created_at,
            updated_at=product.updated_at,
        )


class ProductBulkLookup(BaseModel):
    """Used by the order service to price a cart in one round trip.

    A per-item HTTP call would turn a 10-line order into 10 network hops; this
    keeps order creation to a single dependency call.
    """

    product_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=100)]
