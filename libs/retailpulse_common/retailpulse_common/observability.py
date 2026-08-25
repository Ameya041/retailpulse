"""Structured logging, request correlation IDs and Prometheus metrics.

Metric naming follows the Prometheus convention: ``_total`` for counters,
base-unit seconds for durations. Labels are deliberately low-cardinality --
the route *template* (``/products/{product_id}``) is used rather than the raw
path, otherwise every UUID would create a new time series and blow up storage.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pythonjsonlogger import jsonlogger
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# HTTP metrics (required by the observability spec)
# ---------------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    ["service", "method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["service", "method", "path"],
    # Buckets chosen around the latencies this system actually produces:
    # cache hits land in the low milliseconds, DB writes in the tens.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
HTTP_ERRORS_TOTAL = Counter(
    "http_errors_total",
    "HTTP responses with status >= 400.",
    ["service", "method", "path", "status"],
)
SERVICE_INFO = Gauge(
    "service_up", "1 while the service process is running.", ["service", "version"]
)

# ---------------------------------------------------------------------------
# Business metrics -- incremented by the services that own each concept.
# ---------------------------------------------------------------------------
ORDERS_CREATED_TOTAL = Counter("orders_created_total", "Orders created.", ["service"])
ORDERS_COMPLETED_TOTAL = Counter(
    "orders_completed_total", "Orders that reached DELIVERED.", ["service"]
)
ORDERS_FAILED_TOTAL = Counter(
    "orders_failed_total", "Orders that ended CANCELLED.", ["service", "reason"]
)
INVENTORY_RESERVATIONS_TOTAL = Counter(
    "inventory_reservations_total", "Successful inventory reservations.", ["service"]
)
INVENTORY_RESERVATION_FAILURES_TOTAL = Counter(
    "inventory_reservation_failures_total",
    "Reservations rejected (insufficient stock or missing record).",
    ["service", "reason"],
)
KAFKA_EVENTS_PRODUCED_TOTAL = Counter(
    "kafka_events_produced_total", "Events published.", ["service", "topic"]
)
KAFKA_EVENTS_PROCESSED_TOTAL = Counter(
    "kafka_events_processed_total", "Events consumed successfully.", ["service", "topic"]
)
KAFKA_EVENTS_FAILED_TOTAL = Counter(
    "kafka_events_failed_total", "Events that exhausted retries.", ["service", "topic"]
)
KAFKA_EVENTS_DUPLICATE_TOTAL = Counter(
    "kafka_events_duplicate_total",
    "Events skipped because their event_id was already processed.",
    ["service", "topic"],
)
CACHE_OPERATIONS_TOTAL = Counter(
    "cache_operations_total", "Cache lookups by outcome.", ["service", "result"]
)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """JSON logs to stdout.

    Containers should log to stdout and let the platform handle shipping;
    JSON means Grafana/Loki can filter on ``service`` and ``request_id``
    without regex-parsing free text.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; route them through ours.
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers = [handler]
        logging.getLogger(noisy).propagate = False

    logging.getLogger(service_name).info("Logging configured", extra={"service": service_name})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns/propagates a request ID and records the HTTP metrics."""

    def __init__(self, app: FastAPI, service_name: str) -> None:
        super().__init__(app)
        self.service_name = service_name
        self._log = logging.getLogger(service_name)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour an inbound ID so a trace survives gateway -> service hops.
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            path = self._route_template(request)
            HTTP_REQUESTS_TOTAL.labels(
                self.service_name, request.method, path, "500"
            ).inc()
            HTTP_ERRORS_TOTAL.labels(
                self.service_name, request.method, path, "500"
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                self.service_name, request.method, path
            ).observe(elapsed)
            self._log.exception(
                "Unhandled exception",
                extra={"request_id": request_id, "path": request.url.path},
            )
            raise

        elapsed = time.perf_counter() - started
        path = self._route_template(request)
        status = str(response.status_code)

        HTTP_REQUESTS_TOTAL.labels(self.service_name, request.method, path, status).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            self.service_name, request.method, path
        ).observe(elapsed)
        if response.status_code >= 400:
            HTTP_ERRORS_TOTAL.labels(
                self.service_name, request.method, path, status
            ).inc()

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed * 1000:.2f}"

        self._log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "route": path,
                "status": response.status_code,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
        return response

    @staticmethod
    def _route_template(request: Request) -> str:
        """Return the matched route template, keeping label cardinality bounded."""
        route = request.scope.get("route")
        return getattr(route, "path", None) or "unmatched"


def metrics_endpoint() -> Response:
    """Prometheus scrape target."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
