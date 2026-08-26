"""Route table and the circuit breaker.

## Why a gateway at all

Without one the browser needs the address of six services, CORS has to be
configured six times, and rate limiting and auth are re-implemented six times
with six chances to get them wrong. The gateway gives the frontend one origin
and puts the cross-cutting concerns in one place.

**It contains no business logic.** It routes, authenticates, limits and logs.
The moment a gateway starts making decisions about orders it becomes a
distributed monolith that every team has to deploy through.

## The circuit breaker

If the payment service is down, every proxied request to it waits for the full
timeout before failing. Those waits occupy gateway connections, so a single
sick backend can exhaust the gateway and take down the healthy services with
it. This is the classic cascading failure.

The breaker cuts that off. After a threshold of consecutive failures it OPENS
and requests to that backend fail instantly with 503 rather than waiting.
After a cool-down it goes HALF_OPEN and lets one probe through: success closes
it, failure re-opens it.

    CLOSED --failures exceed threshold--> OPEN
    OPEN --cool-down elapses--> HALF_OPEN
    HALF_OPEN --probe succeeds--> CLOSED
    HALF_OPEN --probe fails--> OPEN

Failing fast is better than failing slow: the caller gets a clear answer
immediately, and the struggling backend gets a chance to recover instead of
being hammered by retries.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("api-gateway")


@dataclass(frozen=True)
class Backend:
    name: str
    prefix: str          # public path prefix, e.g. /api/products
    target_path: str     # path on the upstream service, e.g. /products
    url_setting: str     # settings attribute holding the base URL
    # Reads are safe to retry; writes are not, unless the endpoint is
    # idempotent. The gateway does not know which, so it never retries.
    public_read: bool = True


#: The public API surface. Order matters only in that longer prefixes must be
#: matched before shorter ones -- see `resolve`.
BACKENDS: tuple[Backend, ...] = (
    Backend("product-service", "/api/products", "/products", "product_service_url"),
    Backend("product-service", "/api/categories", "/categories", "product_service_url"),
    Backend("inventory-service", "/api/inventory", "/inventory", "inventory_service_url"),
    Backend("inventory-service", "/api/locations", "/locations", "inventory_service_url"),
    Backend("order-service", "/api/orders", "/orders", "order_service_url", public_read=False),
    Backend("order-service", "/api/cart", "/cart", "order_service_url", public_read=False),
    Backend("user-service", "/api/auth", "/auth", "user_service_url"),
    Backend("user-service", "/api/users", "/users", "user_service_url", public_read=False),
    Backend("payment-service", "/api/payments", "/payments", "payment_service_url", public_read=False),
    Backend("fulfilment-service", "/api/fulfilment", "/fulfilment", "fulfilment_service_url", public_read=False),
    Backend("analytics-service", "/api/analytics", "/analytics", "analytics_service_url", public_read=False),
    Backend("ml-service", "/api/forecast", "/forecast", "ml_service_url", public_read=False),
)


def resolve(path: str) -> tuple[Backend, str] | None:
    """Map a public path to (backend, upstream path).

    Longest prefix wins, so ``/api/products`` cannot swallow a future
    ``/api/products-admin``.
    """
    matches = [b for b in BACKENDS if path == b.prefix or path.startswith(b.prefix + "/")]
    if not matches:
        return None
    backend = max(matches, key=lambda b: len(b.prefix))
    suffix = path[len(backend.prefix) :]
    return backend, f"{backend.target_path}{suffix}"


class BreakerState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreaker:
    """One breaker per backend service."""

    name: str
    failure_threshold: int = 5
    cool_down_seconds: float = 15.0
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = field(default=None)
    _clock: object = field(default=time.monotonic, repr=False)

    def allows_request(self) -> bool:
        if self.state is BreakerState.CLOSED:
            return True

        if self.state is BreakerState.OPEN:
            elapsed = self._clock() - (self.opened_at or 0)
            if elapsed >= self.cool_down_seconds:
                # Cool-down elapsed: let exactly one probe through.
                self.state = BreakerState.HALF_OPEN
                logger.info("circuit half-open", extra={"backend": self.name})
                return True
            return False

        # HALF_OPEN: the probe is already in flight, hold everything else back
        # so a struggling backend is not hit by a stampede.
        return False

    def record_success(self) -> None:
        if self.state is not BreakerState.CLOSED:
            logger.info("circuit closed", extra={"backend": self.name})
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1

        if self.state is BreakerState.HALF_OPEN:
            # The probe failed; give it another full cool-down.
            self._open()
            return

        if self.consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        if self.state is not BreakerState.OPEN:
            logger.warning(
                "circuit opened",
                extra={"backend": self.name, "failures": self.consecutive_failures},
            )
        self.state = BreakerState.OPEN
        self.opened_at = self._clock()

    def snapshot(self) -> dict:
        return {
            "backend": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
        }


class BreakerRegistry:
    """Holds one breaker per backend name."""

    def __init__(self, *, failure_threshold: int = 5, cool_down_seconds: float = 15.0, clock=time.monotonic):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._cool_down_seconds = cool_down_seconds
        self._clock = clock

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=self._failure_threshold,
                cool_down_seconds=self._cool_down_seconds,
                _clock=self._clock,
            )
        return self._breakers[name]

    def snapshot(self) -> list[dict]:
        return [b.snapshot() for b in self._breakers.values()]

    def reset(self) -> None:
        self._breakers.clear()
