"""Domain errors and the single JSON error shape every service returns.

One error envelope means the frontend and the gateway parse one thing, and the
HTTP status is chosen by the domain layer rather than scattered through route
handlers.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class RetailPulseError(Exception):
    """Base class for all expected, domain-level failures.

    Unexpected exceptions deliberately do NOT subclass this -- they surface as
    500s and are logged with a stack trace, because they are bugs, not
    business outcomes.
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(RetailPulseError):
    status_code = 404
    code = "not_found"


class ValidationError(RetailPulseError):
    status_code = 400
    code = "validation_error"


class UnauthorizedError(RetailPulseError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(RetailPulseError):
    status_code = 403
    code = "forbidden"


class ConflictError(RetailPulseError):
    """The request is valid but conflicts with current state.

    Used for duplicate SKUs, illegal order state transitions, and re-submitted
    idempotency keys.
    """

    status_code = 409
    code = "conflict"


class InsufficientInventoryError(ConflictError):
    """Requested quantity exceeds what is available at the location."""

    code = "insufficient_inventory"


class RateLimitedError(RetailPulseError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailableError(RetailPulseError):
    """A dependency (DB, Kafka, Redis, downstream service) is unreachable."""

    status_code = 503
    code = "service_unavailable"


def _envelope(
    *, code: str, message: str, details: dict[str, Any] | None, request: Request
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "path": request.url.path,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so every failure leaves the service in one shape."""

    @app.exception_handler(RetailPulseError)
    async def _domain_error(request: Request, exc: RetailPulseError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                code=exc.code, message=exc.message, details=exc.details, request=request
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope(
                code="validation_error",
                message="Request payload failed validation.",
                # Pydantic echoes the offending input back in each error, and
                # that value can be a Decimal, date or Enum -- none of which the
                # stdlib JSON encoder handles. jsonable_encoder coerces them.
                details={"errors": jsonable_encoder(exc.errors())},
                request=request,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                code=f"http_{exc.status_code}",
                message=str(exc.detail),
                details={},
                request=request,
            ),
            headers=getattr(exc, "headers", None),
        )
