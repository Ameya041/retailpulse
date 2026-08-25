"""Product catalog persistence model.

Schema decisions:

* UUID primary keys rather than serial integers. Sequential IDs leak business
  volume to anyone who can read a URL, and they collide when rows are created
  in more than one place -- a real concern once services write concurrently.
* ``categories`` is a real table with a foreign key rather than a free-text
  column, so category renames touch one row and filtering hits an index.
* Money is ``NUMERIC(12, 2)``, never ``FLOAT``. Binary floating point cannot
  represent 0.10 exactly, and prices that are summed into order totals must not
  drift.
* Status is a constrained string rather than a native Postgres ENUM. Adding a
  value to a native enum needs a DDL migration and locks; a CHECK constraint is
  just as safe and far easier to evolve.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from retailpulse_common.db import Base


class ProductStatus(str, enum.Enum):
    """Lifecycle of a catalog entry.

    Products are never hard-deleted -- ``DELETE /products/{id}`` flips status to
    DISCONTINUED. Orders reference products forever, so a real delete would
    orphan historical order lines and break analytics.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DISCONTINUED = "DISCONTINUED"


class Category(Base):
    __tablename__ = "categories"

    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    products: Mapped[list[Product]] = relationship(back_populates="category")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Category {self.slug}>"


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # SKU is the business key. The unique constraint is the only thing that
    # actually prevents duplicates under concurrency -- an application-level
    # "does it exist?" check races with a second request doing the same check.
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("categories.category_id", ondelete="RESTRICT"), nullable=False
    )
    brand: Mapped[str | None] = mapped_column(String(120), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    weight_grams: Mapped[int | None] = mapped_column()
    status: Mapped[ProductStatus] = mapped_column(
        String(20), nullable=False, default=ProductStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    category: Mapped[Category] = relationship(back_populates="products", lazy="joined")

    __table_args__ = (
        # Data integrity enforced by the database, not just by Pydantic. The
        # API is not the only writer -- migrations, back-fills and psql are too.
        CheckConstraint("price >= 0", name="ck_products_price_non_negative"),
        CheckConstraint(
            "weight_grams IS NULL OR weight_grams >= 0",
            name="ck_products_weight_non_negative",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'DISCONTINUED')",
            name="ck_products_status_valid",
        ),
        CheckConstraint("length(currency) = 3", name="ck_products_currency_iso"),
        # Catalog browsing is "active products in category X, newest first",
        # so the index covers exactly that access path.
        Index("ix_products_category_status", "category_id", "status"),
        Index("ix_products_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Product {self.sku} {self.name!r}>"
