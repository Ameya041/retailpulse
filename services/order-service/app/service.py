"""Cart and order business logic."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Cart, CartItem, Order, OrderItem, OrderStatusHistory
from app.order_state import (
    CUSTOMER_CANCELLABLE,
    InvalidTransitionError,
    OrderStatus,
    is_terminal,
    next_states,
    validate_transition,
)
from app.product_client import CatalogProduct, ProductCatalog
from app.schemas import OrderCreateRequest, OrderLineRequest
from retailpulse_common.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from retailpulse_common.observability import (
    ORDERS_COMPLETED_TOTAL,
    ORDERS_CREATED_TOTAL,
    ORDERS_FAILED_TOTAL,
)

logger = logging.getLogger("order-service")
SERVICE = "order-service"

MAX_ORDER_LINES = 50


class CartService:
    def __init__(self, session: Session, catalog: ProductCatalog) -> None:
        self.session = session
        self.catalog = catalog

    def _get_or_create_cart(self, customer_id: uuid.UUID) -> Cart:
        cart = self.session.scalar(
            select(Cart)
            .where(Cart.customer_id == customer_id)
            .options(selectinload(Cart.items))
        )
        if cart is not None:
            return cart

        cart = Cart(customer_id=customer_id)
        self.session.add(cart)
        try:
            self.session.flush()
        except IntegrityError:
            # Two concurrent "add to cart" requests from the same customer.
            # The unique constraint decides; re-read the winner.
            self.session.rollback()
            cart = self.session.scalar(select(Cart).where(Cart.customer_id == customer_id))
            if cart is None:  # pragma: no cover - only on a genuine DB fault
                raise
        return cart

    def get_cart(self, customer_id: uuid.UUID) -> tuple[Cart, dict[uuid.UUID, CatalogProduct]]:
        cart = self._get_or_create_cart(customer_id)
        product_ids = [item.product_id for item in cart.items]
        return cart, self.catalog.get_many(product_ids)

    def add_item(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, quantity: int
    ) -> Cart:
        # Validate against the catalog before writing: a cart full of
        # non-existent products fails confusingly later, at checkout.
        products = self.catalog.get_many([product_id])
        product = products.get(product_id)
        if product is None:
            raise NotFoundError(
                "That product does not exist.", details={"product_id": str(product_id)}
            )
        if not product.is_orderable:
            raise ConflictError(
                "That product is no longer available.",
                details={"product_id": str(product_id), "status": product.status},
            )

        cart = self._get_or_create_cart(customer_id)
        existing = next((i for i in cart.items if i.product_id == product_id), None)
        if existing is not None:
            # Adding the same product again increases the quantity rather than
            # creating a duplicate line.
            new_quantity = existing.quantity + quantity
            if new_quantity > 100:
                raise ValidationError(
                    "Cart quantity for a single product cannot exceed 100.",
                    details={"product_id": str(product_id), "requested": new_quantity},
                )
            existing.quantity = new_quantity
        else:
            if len(cart.items) >= MAX_ORDER_LINES:
                raise ValidationError(
                    f"A cart cannot hold more than {MAX_ORDER_LINES} distinct products."
                )
            cart.items.append(CartItem(product_id=product_id, quantity=quantity))

        self.session.flush()
        self.session.refresh(cart)
        return cart

    def update_item(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, quantity: int
    ) -> Cart:
        cart = self._get_or_create_cart(customer_id)
        item = next((i for i in cart.items if i.product_id == product_id), None)
        if item is None:
            raise NotFoundError(
                "That product is not in your cart.",
                details={"product_id": str(product_id)},
            )

        if quantity == 0:
            cart.items.remove(item)
        else:
            item.quantity = quantity

        self.session.flush()
        self.session.refresh(cart)
        return cart

    def clear(self, customer_id: uuid.UUID) -> Cart:
        cart = self._get_or_create_cart(customer_id)
        cart.items.clear()
        self.session.flush()
        self.session.refresh(cart)
        return cart


class OrderService:
    def __init__(self, session: Session, catalog: ProductCatalog) -> None:
        self.session = session
        self.catalog = catalog

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, order_id: uuid.UUID) -> Order:
        order = self.session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .options(selectinload(Order.items), selectinload(Order.transitions))
        )
        if order is None:
            raise NotFoundError(
                f"Order {order_id} was not found.", details={"order_id": str(order_id)}
            )
        return order

    def get_for_customer(self, order_id: uuid.UUID, customer_id: uuid.UUID) -> Order:
        """Fetch an order, enforcing ownership.

        Returns 404 rather than 403 when the order belongs to someone else:
        confirming that an order ID exists is itself an information leak.
        """
        order = self.get(order_id)
        if order.customer_id != customer_id:
            raise NotFoundError(
                f"Order {order_id} was not found.", details={"order_id": str(order_id)}
            )
        return order

    def list_for_customer(
        self, customer_id: uuid.UUID, *, offset: int = 0, limit: int = 20
    ) -> tuple[list[Order], int]:
        base = select(Order).where(Order.customer_id == customer_id)
        total = self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = self.session.scalars(
            base.options(selectinload(Order.items))
            .order_by(Order.created_at.desc(), Order.order_id)
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), total

    def list_all(
        self,
        *,
        status: OrderStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Order], int]:
        base = select(Order)
        if status is not None:
            base = base.where(Order.status == status.value)
        total = self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = self.session.scalars(
            base.options(selectinload(Order.items))
            .order_by(Order.created_at.desc(), Order.order_id)
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), total

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create(
        self, customer_id: uuid.UUID, payload: OrderCreateRequest
    ) -> Order:
        lines = payload.lines
        cart: Cart | None = None

        if lines is None:
            cart = self.session.scalar(
                select(Cart)
                .where(Cart.customer_id == customer_id)
                .options(selectinload(Cart.items))
            )
            if cart is None or not cart.items:
                raise ValidationError("Your cart is empty.")
            lines = [
                OrderLineRequest(product_id=i.product_id, quantity=i.quantity)
                for i in cart.items
            ]

        if not lines:
            raise ValidationError("An order must contain at least one line.")
        if len(lines) > MAX_ORDER_LINES:
            raise ValidationError(f"An order cannot exceed {MAX_ORDER_LINES} lines.")

        products = self.catalog.get_many([line.product_id for line in lines])

        missing = [str(line.product_id) for line in lines if line.product_id not in products]
        if missing:
            raise ValidationError(
                "One or more products in this order do not exist.",
                details={"unknown_product_ids": missing},
            )

        unavailable = [
            products[line.product_id].sku
            for line in lines
            if not products[line.product_id].is_orderable
        ]
        if unavailable:
            raise ConflictError(
                "One or more products are no longer available.",
                details={"unavailable_skus": unavailable},
            )

        currencies = {products[line.product_id].currency for line in lines}
        if len(currencies) > 1:
            # Summing across currencies would produce a meaningless total.
            raise ValidationError(
                "All products in an order must share a currency.",
                details={"currencies": sorted(currencies)},
            )

        order = Order(
            customer_id=customer_id,
            status=OrderStatus.CREATED.value,
            currency=currencies.pop(),
            shipping_address=payload.shipping_address.strip(),
            total_amount=Decimal("0.00"),
        )

        total = Decimal("0.00")
        for line in lines:
            product = products[line.product_id]
            # Price is snapshotted here. A later catalog price change must not
            # alter what this customer agreed to pay.
            subtotal = (product.price * line.quantity).quantize(Decimal("0.01"))
            total += subtotal
            order.items.append(
                OrderItem(
                    product_id=product.product_id,
                    product_name=product.name,
                    sku=product.sku,
                    quantity=line.quantity,
                    unit_price=product.price,
                    subtotal=subtotal,
                )
            )

        order.total_amount = total
        order.transitions.append(
            OrderStatusHistory(
                from_status=None, to_status=OrderStatus.CREATED.value, actor="customer"
            )
        )
        self.session.add(order)
        self.session.flush()

        # The cart is consumed by a successful checkout.
        if cart is not None:
            cart.items.clear()

        self.session.flush()
        self.session.refresh(order)

        ORDERS_CREATED_TOTAL.labels(SERVICE).inc()
        logger.info(
            "order created",
            extra={
                "order_id": str(order.order_id),
                "customer_id": str(customer_id),
                "lines": len(order.items),
                "total": str(total),
            },
        )
        return order

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def transition(
        self,
        order_id: uuid.UUID,
        to_status: OrderStatus,
        *,
        actor: str,
        reason: str | None = None,
    ) -> Order:
        """Move an order to a new status, or refuse.

        Every status change in the system funnels through here, so the legality
        rules live in exactly one place.
        """
        order = self.get(order_id)
        current = OrderStatus(order.status)

        if current is to_status:
            # Idempotent no-op. Kafka redelivery means the same transition can
            # arrive twice; re-applying it must not be an error, and must not
            # append a duplicate history row.
            logger.info(
                "transition already applied",
                extra={"order_id": str(order_id), "status": to_status.value},
            )
            return order

        try:
            validate_transition(current, to_status)
        except InvalidTransitionError as exc:
            raise ConflictError(
                str(exc),
                details={
                    "order_id": str(order_id),
                    "current_status": current.value,
                    "requested_status": to_status.value,
                    "allowed_next": [s.value for s in next_states(current)],
                },
            ) from exc

        order.status = to_status.value
        if to_status is OrderStatus.CANCELLED and reason:
            order.cancellation_reason = reason

        order.transitions.append(
            OrderStatusHistory(
                from_status=current.value,
                to_status=to_status.value,
                actor=actor,
                reason=reason,
            )
        )
        self.session.flush()

        if to_status is OrderStatus.DELIVERED:
            ORDERS_COMPLETED_TOTAL.labels(SERVICE).inc()
        elif to_status is OrderStatus.CANCELLED:
            ORDERS_FAILED_TOTAL.labels(SERVICE, reason or "unspecified").inc()

        logger.info(
            "order transitioned",
            extra={
                "order_id": str(order_id),
                "from": current.value,
                "to": to_status.value,
                "actor": actor,
            },
        )
        return order

    def cancel_as_customer(
        self, order_id: uuid.UUID, customer_id: uuid.UUID, reason: str
    ) -> Order:
        """Customer-initiated cancellation, allowed only before fulfilment."""
        order = self.get_for_customer(order_id, customer_id)
        current = OrderStatus(order.status)

        if is_terminal(current):
            raise ConflictError(
                f"This order is already {current.value.lower()} and cannot be cancelled.",
                details={"order_id": str(order_id), "status": current.value},
            )

        if current not in CUSTOMER_CANCELLABLE:
            # Past this point the goods are physically moving; undoing it is a
            # returns process, not a status change.
            raise ForbiddenError(
                "This order has already entered fulfilment and can no longer be "
                "cancelled. Please raise a return once it arrives.",
                details={"order_id": str(order_id), "status": current.value},
            )

        return self.transition(order_id, OrderStatus.CANCELLED, actor="customer", reason=reason)
