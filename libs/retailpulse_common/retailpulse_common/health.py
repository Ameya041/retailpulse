"""Liveness and readiness probes.

The distinction matters to Kubernetes and is a common interview question:

* ``/health`` (liveness) answers "is this process alive?" and must never check
  dependencies. If it did, a Redis blip would make Kubernetes kill and restart
  every pod -- turning a degraded dependency into a full outage.
* ``/ready`` (readiness) answers "can this pod serve traffic right now?" and
  does check dependencies. A failing readiness probe pulls the pod out of the
  Service load balancer without restarting it, so it can recover on its own.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Response

DependencyCheck = Callable[[], bool]


def build_health_router(
    service_name: str,
    version: str = "0.1.0",
    checks: dict[str, DependencyCheck] | None = None,
) -> APIRouter:
    """Router exposing ``GET /health`` and ``GET /ready``.

    ``checks`` maps a dependency name (``"database"``, ``"redis"``, ``"kafka"``)
    to a callable returning True when reachable.
    """
    router = APIRouter(tags=["health"])
    checks = checks or {}

    @router.get("/health", summary="Liveness probe")
    def health() -> dict[str, str]:
        """Process-level liveness. Never touches dependencies."""
        return {"status": "alive", "service": service_name, "version": version}

    @router.get("/ready", summary="Readiness probe")
    def ready(response: Response) -> dict[str, object]:
        """Readiness. Returns 503 when any dependency is unreachable."""
        results = {name: check() for name, check in checks.items()}
        all_ok = all(results.values())
        if not all_ok:
            response.status_code = 503
        return {
            "status": "ready" if all_ok else "not_ready",
            "service": service_name,
            "version": version,
            "dependencies": results,
        }

    return router
