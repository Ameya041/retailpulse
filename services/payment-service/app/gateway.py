"""The simulated payment gateway.

No real money moves. What is simulated is the part that matters for the rest of
the system: **a payment provider fails sometimes**, and every downstream
service has to cope with that.

The failure rate is configurable (default 5%) so the compensation path can be
demonstrated on demand rather than waited for.

Two deliberate choices:

* The gateway is behind a Protocol, so tests inject a gateway that always
  succeeds or always fails instead of retrying until the dice cooperate. A test
  suite that depends on randomness is a flaky test suite.
* The RNG is an instance, not the ``random`` module globals. Seeding the global
  RNG from library code would silently change behaviour anywhere else in the
  process that uses randomness.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger("payment-service")

# Realistic-looking decline reasons, weighted the way real ones are: most
# declines are funds or bank-side, not fraud.
DECLINE_REASONS = (
    ("INSUFFICIENT_FUNDS", 45),
    ("CARD_DECLINED_BY_BANK", 25),
    ("CARD_EXPIRED", 12),
    ("GATEWAY_TIMEOUT", 10),
    ("SUSPECTED_FRAUD", 8),
)


@dataclass(frozen=True)
class ChargeResult:
    approved: bool
    transaction_reference: str
    failure_reason: str | None = None


class PaymentGateway(Protocol):
    def charge(
        self, *, order_id: uuid.UUID, amount: Decimal, currency: str, method: str
    ) -> ChargeResult:
        ...


def _reference() -> str:
    """Provider-style reference. Uppercase and unambiguous when read aloud."""
    return f"TXN-{uuid.uuid4().hex[:16].upper()}"


class SimulatedPaymentGateway:
    """Approves a configurable proportion of charges."""

    def __init__(self, success_rate: float = 0.95, seed: int | None = None) -> None:
        if not 0.0 <= success_rate <= 1.0:
            raise ValueError("success_rate must be between 0.0 and 1.0")
        self.success_rate = success_rate
        self._rng = random.Random(seed)  # noqa: S311 - simulation, not crypto

    def charge(
        self,
        *,
        order_id: uuid.UUID,
        amount: Decimal,
        currency: str,
        method: str,  # noqa: ARG002 - part of the protocol; a real gateway routes on it
    ) -> ChargeResult:
        approved = self._rng.random() < self.success_rate
        reference = _reference()

        if approved:
            logger.info(
                "charge approved",
                extra={
                    "order_id": str(order_id),
                    "amount": str(amount),
                    "currency": currency,
                    "reference": reference,
                },
            )
            return ChargeResult(approved=True, transaction_reference=reference)

        reasons, weights = zip(*DECLINE_REASONS, strict=True)
        reason = self._rng.choices(reasons, weights=weights, k=1)[0]
        logger.info(
            "charge declined",
            extra={"order_id": str(order_id), "reason": reason, "reference": reference},
        )
        return ChargeResult(
            approved=False, transaction_reference=reference, failure_reason=reason
        )


class AlwaysApprovesGateway:
    """Test double for the happy path."""

    def charge(self, *, order_id, amount, currency, method) -> ChargeResult:  # noqa: ARG002
        return ChargeResult(approved=True, transaction_reference=_reference())


class AlwaysDeclinesGateway:
    """Test double for the compensation path."""

    def __init__(self, reason: str = "INSUFFICIENT_FUNDS") -> None:
        self.reason = reason

    def charge(self, *, order_id, amount, currency, method) -> ChargeResult:  # noqa: ARG002
        return ChargeResult(
            approved=False, transaction_reference=_reference(), failure_reason=self.reason
        )


class UnavailableGateway:
    """Test double for a provider outage.

    Distinct from a decline: a decline is an answer, an outage is no answer at
    all. The service must retry an outage and must not retry a decline.
    """

    def charge(self, *, order_id, amount, currency, method):  # noqa: ARG002
        raise ConnectionError("payment provider unreachable")
