"""The order state machine.

An order's status is not a free-text field that any handler may overwrite.
It is a node in an explicit directed graph, and every change goes through
:func:`validate_transition`. Without this, a delayed or duplicated Kafka event
could walk an order backwards -- DELIVERED back to CREATED -- and no single
place would be responsible for noticing.

The happy path::

    CREATED -> INVENTORY_RESERVED -> PAYMENT_CONFIRMED -> CONFIRMED
            -> FULFILMENT_STARTED -> SHIPPED -> DELIVERED

Compensating path when payment fails::

    INVENTORY_RESERVED -> PAYMENT_FAILED -> INVENTORY_RELEASED -> CANCELLED

Two properties are worth stating explicitly because interviewers ask:

* **Terminal states are absorbing.** DELIVERED and CANCELLED have no outgoing
  edges at all, so a late duplicate event cannot resurrect a finished order.
* **Cancellation is only legal before the goods move.** Once fulfilment has
  started the physical world is already in motion, and undoing it is a returns
  process, not a status change.
"""

from __future__ import annotations

import enum


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    INVENTORY_RESERVED = "INVENTORY_RESERVED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    CONFIRMED = "CONFIRMED"
    FULFILMENT_STARTED = "FULFILMENT_STARTED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    # Failure / compensation branch
    PAYMENT_FAILED = "PAYMENT_FAILED"
    INVENTORY_RELEASED = "INVENTORY_RELEASED"
    CANCELLED = "CANCELLED"


#: The complete set of legal edges. Anything not listed here is rejected.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.INVENTORY_RESERVED,
            # Reservation failed, or the customer changed their mind before
            # any stock was held.
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.INVENTORY_RESERVED: frozenset(
        {
            OrderStatus.PAYMENT_CONFIRMED,
            OrderStatus.PAYMENT_FAILED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PAYMENT_CONFIRMED: frozenset({OrderStatus.CONFIRMED}),
    OrderStatus.CONFIRMED: frozenset(
        {
            OrderStatus.FULFILMENT_STARTED,
            # Last chance to cancel: nothing has physically shipped yet.
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FULFILMENT_STARTED: frozenset({OrderStatus.SHIPPED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    # Compensation branch: released stock must be explicitly returned before
    # the order is closed, so stock is never silently stranded.
    OrderStatus.PAYMENT_FAILED: frozenset({OrderStatus.INVENTORY_RELEASED}),
    OrderStatus.INVENTORY_RELEASED: frozenset({OrderStatus.CANCELLED}),
    # Absorbing states.
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

#: Statuses from which an order can never move again.
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {status for status, nexts in ALLOWED_TRANSITIONS.items() if not nexts}
)

#: Statuses in which a customer may still cancel of their own accord.
CUSTOMER_CANCELLABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.CREATED, OrderStatus.INVENTORY_RESERVED, OrderStatus.CONFIRMED}
)

#: Statuses that mean stock is currently held for this order.
STOCK_HELD_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.INVENTORY_RESERVED,
        OrderStatus.PAYMENT_CONFIRMED,
        OrderStatus.CONFIRMED,
        OrderStatus.FULFILMENT_STARTED,
    }
)


class InvalidTransitionError(Exception):
    """Raised when a status change is not a legal edge in the graph."""

    def __init__(self, current: OrderStatus, requested: OrderStatus) -> None:
        self.current = current
        self.requested = requested
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        super().__init__(
            f"Cannot move an order from {current.value} to {requested.value}. "
            f"Allowed next states: {allowed or ['(none - terminal)']}."
        )


def can_transition(current: OrderStatus, requested: OrderStatus) -> bool:
    return requested in ALLOWED_TRANSITIONS[current]


def validate_transition(current: OrderStatus, requested: OrderStatus) -> None:
    """Raise unless ``current -> requested`` is a legal edge."""
    if not can_transition(current, requested):
        raise InvalidTransitionError(current, requested)


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


def holds_stock(status: OrderStatus) -> bool:
    """True while inventory is reserved and not yet consumed or returned."""
    return status in STOCK_HELD_STATUSES


def next_states(current: OrderStatus) -> list[OrderStatus]:
    return sorted(ALLOWED_TRANSITIONS[current], key=lambda s: s.value)
