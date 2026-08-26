"""Cart and order persistence models.

Two decisions that matter for correctness:

* **Order lines snapshot the price.** `order_items.unit_price` is copied from
  the catalog at order time, never joined at read time. If a product's price
  changes tomorrow, a customer's past order total must not change with it --
  and the order total must always equal the sum of its lines.
* **The order total is stored, not computed on read.** It is recomputed from
  the lines whenever they change, and a CHECK constraint keeps it non-negative.
  Storing it means analytics and invoices agree with what the customer saw.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from retailpulse_common.db import Base

# Registers `processed_events` and `outbox_events` on this service's metadata.
# Imported here rather than in the modules that use them so Alembic sees the
# tables from a single, obvious place.
from retailpulse_common.events.idempotency import ProcessedEvent  # noqa: F401,E402
from retailpulse_common.events.outbox import OutboxEvent  # noqa: F401,E402

ORDER_STATUSES = (
    "CREATED",
    "INVENTORY_RESERVED",
    "PAYMENT_CONFIRMED",
    "CONFIRMED",
    "FULFILMENT_STARTED",
    "SHIPPED",
    "DELIVERED",
    "PAYMENT_FAILED",
    "INVENTORY_RELEASED",
    "CANCELLED",
)
_STATUS_SQL_LIST = ", ".join(f"'{s}'" for s in ORDER_STATUSES)


class Cart(Base):
    """A customer's working basket. One open cart per customer."""

    __tablename__ = "carts"

    cart_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # One active cart per customer. Two carts would let the UI and the API
        # disagree about what is in the basket.
        UniqueConstraint("customer_id", name="uq_carts_customer"),
    )


class CartItem(Base):
    __tablename__ = "cart_items"

    cart_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cart_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("carts.cart_id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cart: Mapped[Cart] = relationship(back_populates="items")

    __table_args__ = (
        # Adding the same product twice increments the quantity rather than
        # creating a second line.
        UniqueConstraint("cart_id", "product_id", name="uq_cart_items_cart_product"),
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
        Index("ix_cart_items_cart_id", "cart_id"),
    )


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="CREATED")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    # Why the order ended up cancelled -- surfaced to the customer and used by
    # the cancellation-rate metric.
    cancellation_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    transitions: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL_LIST})", name="ck_orders_status_valid"),
        CheckConstraint("total_amount >= 0", name="ck_orders_total_non_negative"),
        CheckConstraint("length(currency) = 3", name="ck_orders_currency_iso"),
        # "My orders, newest first" and "all orders in state X" are the two
        # queries this table actually serves.
        Index("ix_orders_customer_created", "customer_id", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.order_id} {self.status} {self.total_amount}>"


class OrderItem(Base):
    __tablename__ = "order_items"

    order_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Denormalised on purpose: the catalog is another service's database, and
    # an invoice must still render if that service is down or the product was
    # discontinued.
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("order_id", "product_id", name="uq_order_items_order_product"),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_non_negative"),
        CheckConstraint("subtotal >= 0", name="ck_order_items_subtotal_non_negative"),
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_product_id", "product_id"),
    )


class OrderStatusHistory(Base):
    """Every status change, with who or what caused it.

    Orders move through a distributed saga driven by events from several
    services. When an order is stuck, this table answers "how did it get
    here?" without correlating logs across five services.
    """

    __tablename__ = "order_status_history"

    history_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    # "customer", "payment-service", "fulfilment-service", ...
    actor: Mapped[str] = mapped_column(String(48), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="transitions")

    __table_args__ = (
        CheckConstraint(f"to_status IN ({_STATUS_SQL_LIST})", name="ck_history_to_status_valid"),
        Index("ix_order_status_history_order", "order_id", "created_at"),
    )
