"""The reverse proxy itself."""

from __future__ import annotations

import logging
import time

import httpx
from fastapi import Request, Response

from app.routing import Backend, BreakerRegistry
from retailpulse_common.errors import ServiceUnavailableError
from retailpulse_common.observability import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
)

logger = logging.getLogger("api-gateway")

SERVICE = "api-gateway"

# Headers that describe *this* connection and must not be forwarded to the
# upstream, which has its own. Forwarding hop-by-hop headers is a classic
# proxy bug: a `Content-Length` copied onto a re-encoded body, or a
# `Connection: keep-alive` applied to the wrong socket, produces corrupted
# responses that are very hard to trace.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


def _forwardable(headers, extra: dict[str, str]) -> dict[str, str]:
    """Copy headers, dropping hop-by-hop ones and applying overrides.

    Keys are normalised to lower case before merging. HTTP header names are
    case-insensitive, so an incoming `x-request-id` and an override spelled
    `X-Request-ID` are the same header -- but they are different dict keys, and
    keeping both makes the client emit the value twice as a comma-joined
    multi-value header.
    """
    forwarded = {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    forwarded.update({key.lower(): value for key, value in extra.items()})
    # Never forward an empty value: some servers treat a blank header as
    # malformed rather than absent.
    return {key: value for key, value in forwarded.items() if value}


async def proxy(
    request: Request,
    *,
    backend: Backend,
    upstream_path: str,
    base_url: str,
    client: httpx.AsyncClient,
    breakers: BreakerRegistry,
) -> Response:
    """Forward a request to a backend and return its response verbatim."""
    breaker = breakers.get(backend.name)

    if not breaker.allows_request():
        # Fail fast rather than queueing behind a backend already known to be
        # failing. This is what stops one sick service exhausting the gateway.
        logger.warning(
            "request rejected by open circuit",
            extra={"backend": backend.name, "path": request.url.path},
        )
        raise ServiceUnavailableError(
            f"{backend.name} is temporarily unavailable. Please retry shortly.",
            details={"backend": backend.name, "circuit": breaker.state.value},
        )

    url = f"{base_url.rstrip('/')}{upstream_path}"
    request_id = getattr(request.state, "request_id", "")

    headers = _forwardable(
        request.headers,
        {
            # Propagated so one request ID threads through the gateway and every
            # service it touches -- without it, correlating logs across six
            # services means guessing from timestamps.
            "X-Request-ID": request_id,
            "X-Forwarded-For": request.client.host if request.client else "",
            "X-Forwarded-Proto": request.url.scheme,
        },
    )

    body = await request.body()
    started = time.perf_counter()

    try:
        upstream = await client.request(
            method=request.method,
            url=url,
            params=dict(request.query_params),
            headers=headers,
            content=body,
        )
    except httpx.TimeoutException as exc:
        breaker.record_failure()
        elapsed = time.perf_counter() - started
        _record(backend, request, "504", elapsed)
        logger.warning(
            "upstream timed out",
            extra={"backend": backend.name, "url": url, "elapsed_ms": round(elapsed * 1000)},
        )
        raise ServiceUnavailableError(
            f"{backend.name} did not respond in time.",
            details={"backend": backend.name},
        ) from exc
    except httpx.HTTPError as exc:
        breaker.record_failure()
        _record(backend, request, "503", time.perf_counter() - started)
        logger.warning(
            "upstream unreachable", extra={"backend": backend.name, "error": str(exc)}
        )
        raise ServiceUnavailableError(
            f"{backend.name} is unreachable.", details={"backend": backend.name}
        ) from exc

    # A 5xx from the backend counts against the breaker; a 4xx does not. A 404
    # or a 422 means the backend is healthy and doing its job -- tripping the
    # breaker on client errors would take a service offline because someone
    # typo'd a URL.
    if upstream.status_code >= 500:
        breaker.record_failure()
    else:
        breaker.record_success()

    elapsed = time.perf_counter() - started
    _record(backend, request, str(upstream.status_code), elapsed)

    response_headers = _forwardable(upstream.headers, {"X-Request-ID": request_id})
    response_headers["X-Gateway-Backend"] = backend.name

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


def _record(backend: Backend, request: Request, status: str, elapsed: float) -> None:
    """Metrics labelled by backend prefix, not by raw path.

    The prefix keeps label cardinality bounded; the raw path would create a
    time series per product ID.
    """
    HTTP_REQUESTS_TOTAL.labels(SERVICE, request.method, backend.prefix, status).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(SERVICE, request.method, backend.prefix).observe(elapsed)
