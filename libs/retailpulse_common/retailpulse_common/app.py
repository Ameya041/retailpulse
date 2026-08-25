"""Application factory.

Every service builds its FastAPI app through :func:`create_service_app` so
that logging, metrics, error shapes, CORS, health probes and API docs are
identical everywhere. A service then only adds its own routers.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from retailpulse_common.config import ServiceSettings
from retailpulse_common.errors import register_exception_handlers
from retailpulse_common.health import build_health_router
from retailpulse_common.observability import (
    SERVICE_INFO,
    RequestContextMiddleware,
    configure_logging,
    metrics_endpoint,
)

VERSION = "0.1.0"


def create_service_app(
    *,
    settings: ServiceSettings,
    title: str,
    description: str,
    checks: dict[str, Callable[[], bool]] | None = None,
    on_startup: Callable[[], None] | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        SERVICE_INFO.labels(settings.service_name, VERSION).set(1)
        if on_startup is not None:
            on_startup()
        yield
        if on_shutdown is not None:
            on_shutdown()
        SERVICE_INFO.labels(settings.service_name, VERSION).set(0)

    app = FastAPI(
        title=title,
        description=description,
        version=VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware, service_name=settings.service_name)
    app.add_middleware(
        CORSMiddleware,
        # Explicit origins, not "*": with credentialed requests a wildcard is
        # rejected by browsers, and an open CORS policy is a real finding in a
        # security review.
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-Ms", "Retry-After"],
    )

    register_exception_handlers(app)
    app.include_router(build_health_router(settings.service_name, VERSION, checks))

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        """Prometheus scrape endpoint."""
        return metrics_endpoint()

    @app.get("/swagger", include_in_schema=False)
    def swagger():
        """Alias so /swagger and /docs both work."""
        return RedirectResponse(url="/docs")

    @app.get("/", include_in_schema=False)
    def root():
        return {
            "service": settings.service_name,
            "version": VERSION,
            "environment": settings.environment,
            "docs": "/docs",
        }

    return app
