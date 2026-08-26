"""Exhaustive tests for the order state machine.

The graph is small enough to test completely, so these tests enumerate every
(from, to) pair rather than sampling. That is the point of modelling status as
an explicit graph: its correctness is finitely checkable.
"""

from __future__ import annotations

import itertools

import pytest

from app.order_state import (
    ALLOWED_TRANSITIONS,
    CUSTOMER_CANCELLABLE,
    TERMINAL_STATUSES,
    InvalidTransitionError,
    OrderStatus,
    can_transition,
    holds_stock,
    is_terminal,
    next_states,
    validate_transition,
)

HAPPY_PATH = [
    OrderStatus.CREATED,
    OrderStatus.INVENTORY_RESERVED,
    OrderStatus.PAYMENT_CONFIRMED,
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILMENT_STARTED,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
]

PAYMENT_FAILURE_PATH = [
    OrderStatus.CREATED,
    OrderStatus.INVENTORY_RESERVED,
    OrderStatus.PAYMENT_FAILED,
    OrderStatus.INVENTORY_RELEASED,
    OrderStatus.CANCELLED,
]


# ---------------------------------------------------------------------------
# Graph completeness
# ---------------------------------------------------------------------------
def test_every_status_appears_in_the_transition_table():
    """A status with no entry would raise KeyError at runtime."""
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


def test_every_target_status_is_itself_a_valid_status():
    for targets in ALLOWED_TRANSITIONS.values():
        for target in targets:
            assert target in OrderStatus


def test_no_status_transitions_to_itself():
    """Self-edges are handled as idempotent no-ops, not as graph edges."""
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets


def test_every_non_terminal_status_is_reachable_from_created():
    """No orphan states: anything defined must be reachable in practice."""
    reachable = {OrderStatus.CREATED}
    frontier = [OrderStatus.CREATED]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)

    assert reachable == set(OrderStatus)


def test_every_status_can_reach_a_terminal_state():
    """No dead ends: an order must always be able to finish."""
    for start in OrderStatus:
        seen = {start}
        frontier = [start]
        found_terminal = False
        while frontier:
            current = frontier.pop()
            if is_terminal(current):
                found_terminal = True
                break
            for nxt in ALLOWED_TRANSITIONS[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert found_terminal, f"{start.value} can never reach a terminal state"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "following"), list(itertools.pairwise(HAPPY_PATH))
)
def test_happy_path_transitions_are_allowed(current, following):
    validate_transition(current, following)  # must not raise


@pytest.mark.parametrize(
    ("current", "following"), list(itertools.pairwise(PAYMENT_FAILURE_PATH))
)
def test_payment_failure_path_transitions_are_allowed(current, following):
    validate_transition(current, following)


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------
def test_delivered_cannot_go_back_to_created():
    """The example the spec calls out explicitly."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.DELIVERED, OrderStatus.CREATED)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(OrderStatus))
def test_terminal_states_absorb_everything(terminal, target):
    """No edge leaves a terminal state, for any target at all."""
    assert not can_transition(terminal, target)


def test_cannot_skip_payment():
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.INVENTORY_RESERVED, OrderStatus.CONFIRMED)


def test_cannot_ship_before_fulfilment_starts():
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.CONFIRMED, OrderStatus.SHIPPED)


def test_cannot_reserve_inventory_twice_by_going_backwards():
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.PAYMENT_CONFIRMED, OrderStatus.INVENTORY_RESERVED)


def test_cannot_cancel_once_shipped():
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.SHIPPED, OrderStatus.CANCELLED)


def test_cannot_cancel_during_fulfilment():
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.FULFILMENT_STARTED, OrderStatus.CANCELLED)


def test_payment_failure_must_release_inventory_before_cancelling():
    """Skipping INVENTORY_RELEASED would strand held stock forever."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(OrderStatus.PAYMENT_FAILED, OrderStatus.CANCELLED)

    validate_transition(OrderStatus.PAYMENT_FAILED, OrderStatus.INVENTORY_RELEASED)


def test_exhaustive_pairs_match_the_transition_table():
    """Every one of the 100 (from, to) pairs behaves exactly as declared."""
    for current, target in itertools.product(OrderStatus, repeat=2):
        expected = target in ALLOWED_TRANSITIONS[current]
        assert can_transition(current, target) is expected


# ---------------------------------------------------------------------------
# Error message quality
# ---------------------------------------------------------------------------
def test_error_names_both_states_and_lists_what_is_allowed():
    with pytest.raises(InvalidTransitionError) as exc:
        validate_transition(OrderStatus.CREATED, OrderStatus.DELIVERED)

    message = str(exc.value)
    assert "CREATED" in message
    assert "DELIVERED" in message
    assert "INVENTORY_RESERVED" in message  # tells the caller what IS possible


def test_terminal_error_message_says_terminal():
    with pytest.raises(InvalidTransitionError) as exc:
        validate_transition(OrderStatus.CANCELLED, OrderStatus.CREATED)
    assert "terminal" in str(exc.value)


# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------
def test_terminal_statuses_are_exactly_delivered_and_cancelled():
    assert {OrderStatus.DELIVERED, OrderStatus.CANCELLED} == TERMINAL_STATUSES


def test_stock_is_held_between_reservation_and_fulfilment():
    assert holds_stock(OrderStatus.INVENTORY_RESERVED)
    assert holds_stock(OrderStatus.CONFIRMED)
    assert holds_stock(OrderStatus.FULFILMENT_STARTED)
    # Before reservation and after the compensation, nothing is held.
    assert not holds_stock(OrderStatus.CREATED)
    assert not holds_stock(OrderStatus.INVENTORY_RELEASED)
    assert not holds_stock(OrderStatus.CANCELLED)
    # SHIPPED means the units were committed, not still reserved.
    assert not holds_stock(OrderStatus.SHIPPED)


def test_customer_cancellable_states_are_all_pre_fulfilment():
    for status in CUSTOMER_CANCELLABLE:
        assert can_transition(status, OrderStatus.CANCELLED)


def test_next_states_is_sorted_and_complete():
    assert next_states(OrderStatus.INVENTORY_RESERVED) == [
        OrderStatus.CANCELLED,
        OrderStatus.PAYMENT_CONFIRMED,
        OrderStatus.PAYMENT_FAILED,
    ]


def test_next_states_of_a_terminal_status_is_empty():
    assert next_states(OrderStatus.DELIVERED) == []
