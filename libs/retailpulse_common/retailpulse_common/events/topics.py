"""Topic and event-type registry.

Topic names live here rather than as string literals at call sites, so a typo
is an ImportError at startup instead of a message silently published to a topic
nobody consumes -- one of the more unpleasant bugs to diagnose in an
event-driven system, because nothing errors.

**Partitioning.** Every order-related event is keyed by ``order_id``. Kafka
guarantees ordering only *within* a partition, and the same key always hashes
to the same partition, so all events for one order are processed in the order
they were emitted. Keying by anything else -- or not at all -- would let
INVENTORY_RESERVED overtake ORDER_CREATED for the same order.

**Dead-letter topics.** Grouped by domain rather than one per source topic.
Per-topic DLQs multiply operational surface for no benefit; what an operator
actually wants is "show me everything the order pipeline could not process".
"""

from __future__ import annotations

from typing import Final


class Topic:
    """Business event topics."""

    ORDER_CREATED: Final = "order.created"
    ORDER_CONFIRMED: Final = "order.confirmed"
    ORDER_CANCELLED: Final = "order.cancelled"
    ORDER_SHIPPED: Final = "order.shipped"
    ORDER_DELIVERED: Final = "order.delivered"

    INVENTORY_RESERVED: Final = "inventory.reserved"
    INVENTORY_RELEASED: Final = "inventory.released"
    # Not in the original topic list, but the saga needs an explicit signal that
    # reservation was rejected. Without it the order service can only infer
    # failure from a timeout, which is slow and ambiguous.
    INVENTORY_FAILED: Final = "inventory.failed"
    INVENTORY_LOW: Final = "inventory.low"

    # The order service asks for payment once stock is held; the payment
    # service answers on one of the two topics below.
    PAYMENT_REQUESTED: Final = "payment.requested"
    PAYMENT_CONFIRMED: Final = "payment.confirmed"
    PAYMENT_FAILED: Final = "payment.failed"

    FULFILMENT_STARTED: Final = "fulfilment.started"

    PRODUCT_UPDATED: Final = "product.updated"


class DeadLetterTopic:
    ORDERS: Final = "orders.dlq"
    INVENTORY: Final = "inventory.dlq"
    PAYMENTS: Final = "payments.dlq"


class EventType:
    """Business event names carried in the envelope's ``event_type``."""

    ORDER_CREATED: Final = "ORDER_CREATED"
    ORDER_CONFIRMED: Final = "ORDER_CONFIRMED"
    ORDER_CANCELLED: Final = "ORDER_CANCELLED"
    ORDER_SHIPPED: Final = "ORDER_SHIPPED"
    ORDER_DELIVERED: Final = "ORDER_DELIVERED"

    INVENTORY_RESERVED: Final = "INVENTORY_RESERVED"
    INVENTORY_RELEASED: Final = "INVENTORY_RELEASED"
    INVENTORY_FAILED: Final = "INVENTORY_FAILED"
    INVENTORY_LOW: Final = "INVENTORY_LOW"

    PAYMENT_REQUESTED: Final = "PAYMENT_REQUESTED"
    PAYMENT_CONFIRMED: Final = "PAYMENT_CONFIRMED"
    PAYMENT_FAILED: Final = "PAYMENT_FAILED"

    FULFILMENT_STARTED: Final = "FULFILMENT_STARTED"

    PRODUCT_UPDATED: Final = "PRODUCT_UPDATED"


#: Which dead-letter topic a failed message from each topic belongs in.
DLQ_ROUTING: Final[dict[str, str]] = {
    Topic.ORDER_CREATED: DeadLetterTopic.ORDERS,
    Topic.ORDER_CONFIRMED: DeadLetterTopic.ORDERS,
    Topic.ORDER_CANCELLED: DeadLetterTopic.ORDERS,
    Topic.ORDER_SHIPPED: DeadLetterTopic.ORDERS,
    Topic.ORDER_DELIVERED: DeadLetterTopic.ORDERS,
    Topic.INVENTORY_RESERVED: DeadLetterTopic.INVENTORY,
    Topic.INVENTORY_RELEASED: DeadLetterTopic.INVENTORY,
    Topic.INVENTORY_FAILED: DeadLetterTopic.INVENTORY,
    Topic.INVENTORY_LOW: DeadLetterTopic.INVENTORY,
    Topic.PAYMENT_REQUESTED: DeadLetterTopic.PAYMENTS,
    Topic.PAYMENT_CONFIRMED: DeadLetterTopic.PAYMENTS,
    Topic.PAYMENT_FAILED: DeadLetterTopic.PAYMENTS,
    Topic.FULFILMENT_STARTED: DeadLetterTopic.ORDERS,
    Topic.PRODUCT_UPDATED: DeadLetterTopic.ORDERS,
}

#: Every business topic that must exist before the services start.
ALL_TOPICS: Final[tuple[str, ...]] = tuple(DLQ_ROUTING)

#: Every dead-letter topic.
ALL_DLQ_TOPICS: Final[tuple[str, ...]] = (
    DeadLetterTopic.ORDERS,
    DeadLetterTopic.INVENTORY,
    DeadLetterTopic.PAYMENTS,
)


def dlq_for(topic: str) -> str:
    """Dead-letter topic for a source topic.

    Falls back to the orders DLQ rather than raising: losing a message because
    its DLQ was unmapped would be the worst possible outcome of a typo.
    """
    return DLQ_ROUTING.get(topic, DeadLetterTopic.ORDERS)
