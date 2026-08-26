"""API gateway entrypoint.

Single entry point for the frontend. Routes, authenticates, rate limits and
logs -- and holds no business logic of its own.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request, Response

from app.config import get_settings
from app.deps import (
    close_http_client,
    get_breakers,
    get_http_client,
    get_rate_limiter,
    optional_user,
    require_roles,
)
from app.proxy import proxy
from app.routing import BreakerRegistry, resolve
from retailpulse_common.app import create_service_app
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.errors import NotFoundError, RateLimitedError
from retailpulse_common.rate_limit import RateLimiter

settings = get_settings()
logger = logging.getLogger("api-gateway")

router = APIRouter()

ClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
BreakersDep = Annotated[BreakerRegistry, Depends(get_breakers)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
UserDep = Annotated[TokenPayload | None, Depends(optional_user)]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _identity(request: Request, user: TokenPayload | None) -> tuple[str, int]:
    """Who to rate limit, and how generously.

    An authenticated user gets their own bucket keyed by user id. Anonymous
    callers share a bucket per IP with a lower ceiling: an IP is a much weaker
    identity (offices and mobile carriers NAT many people behind one), so the
    limit has to be low enough to blunt abuse without being so low that a
    shared connection breaks normal browsing.
    """
    if user is not None:
        return f"user:{user.sub}", settings.rate_limit_requests_per_minute

    forwarded = request.headers.get("X-Forwarded-For")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    return f"ip:{ip}", settings.anonymous_rate_limit_per_minute


@router.api_route("/api/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def gateway(
    path: str,  # noqa: ARG001 - captured by the route, read from request.url
    request: Request,
    client: ClientDep,
    breakers: BreakersDep,
    limiter: LimiterDep,
    user: UserDep,
) -> Response:
    """Proxy everything under /api to the service that owns it.

    Authentication is *verified* here but not enforced: the gateway decodes a
    token if one is present so requests can be attributed and rate limited per
    user, and then passes the original Authorization header through. Each
    service enforces its own authorization, because a gateway that decided
    permissions would be a single point of privilege and services would be
    wide open to anything that reached them directly.
    """
    match = resolve(request.url.path)
    if match is None:
        raise NotFoundError(
            f"No route matches {request.url.path}.",
            details={"path": request.url.path},
        )
    backend, upstream_path = match

    identity, limit = _identity(request, user)
    result = limiter.check(
        identity, limit=limit, window_seconds=settings.rate_limit_window_seconds
    )
    if not result.allowed:
        logger.warning(
            "rate limit exceeded",
            extra={"identity": identity, "path": request.url.path, "limit": limit},
        )
        raise RateLimitedError(
            "Too many requests. Please slow down.",
            details={
                "limit": result.limit,
                "window_seconds": settings.rate_limit_window_seconds,
                "retry_after_seconds": result.retry_after_seconds,
            },
        )

    response = await proxy(
        request,
        backend=backend,
        upstream_path=upstream_path,
        base_url=getattr(settings, backend.url_setting),
        client=client,
        breakers=breakers,
    )
    # Sent on success too, so a well-behaved client can back off before it is
    # ever rejected.
    response.headers.update(result.headers())
    return response


@router.get("/gateway/routes", tags=["gateway"], summary="Published route table")
def routes() -> dict:
    """What the gateway exposes and which service serves it."""
    from app.routing import BACKENDS

    return {
        "routes": [
            {"prefix": b.prefix, "backend": b.name, "upstream_path": b.target_path}
            for b in BACKENDS
        ]
    }


@router.get("/gateway/circuits", tags=["gateway"], summary="Circuit breaker states")
def circuits(breakers: BreakersDep, _: AdminDep) -> dict:
    """Live breaker state per backend. Requires ADMIN.

    The first thing to look at when the API is returning 503s: it says whether
    the gateway is refusing to call a backend, and which one.
    """
    return {"circuits": breakers.snapshot()}


app = create_service_app(
    settings=settings,
    title="RetailPulse API Gateway",
    description=(
        "Single entry point for the frontend. Routes `/api/*` to the service that "
        "owns each domain, and holds no business logic of its own.\n\n"
        "**Authentication vs authorization.** The gateway verifies a token when one "
        "is present, so requests can be attributed and rate limited per user, then "
        "forwards the original Authorization header. Each service still enforces its "
        "own authorization -- otherwise the gateway would be a single point of "
        "privilege and every service would be wide open to anything reaching it "
        "directly.\n\n"
        "**Rate limiting.** A Redis sliding-window log, evaluated atomically in Lua so "
        "concurrent requests cannot slip past the limit. Authenticated users get their "
        "own bucket; anonymous callers share a smaller per-IP one. If Redis is down the "
        "limiter fails open -- a deliberate trade of strictness for availability.\n\n"
        "**Circuit breakers.** After repeated failures a backend is cut off and requests "
        "to it fail immediately, so one sick service cannot exhaust the gateway's "
        "connections and take the healthy ones down with it."
    ),
    checks={
        # No database, and Redis is intentionally excluded: the limiter fails
        # open, so Redis being down must not pull the gateway out of the load
        # balancer.
        "upstream_config": lambda: bool(settings.product_service_url),
    },
    on_shutdown=lambda: None,
)

app.include_router(router)


@app.on_event("shutdown")
async def _shutdown() -> None:  # pragma: no cover - lifecycle
    await close_http_client()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
