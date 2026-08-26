"""ML service wiring."""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import get_settings
from app.forecaster import DemandForecaster, ModelNotLoadedError
from retailpulse_common.auth import build_auth_dependencies
from retailpulse_common.errors import ServiceUnavailableError

settings = get_settings()
logger = logging.getLogger("ml-service")


@lru_cache(maxsize=1)
def _forecaster() -> DemandForecaster:
    """Loaded once and reused.

    Deserialising the model and reading the history snapshot takes long enough
    that doing it per request would dominate response time.
    """
    return DemandForecaster.load(settings.model_path, settings.history_path)


def get_forecaster() -> DemandForecaster:
    """Overridden in tests with a forecaster built from a small fixture."""
    try:
        return _forecaster()
    except ModelNotLoadedError as exc:
        # A missing artifact is an operational problem, not a client error.
        raise ServiceUnavailableError(
            "The forecasting model is not loaded. Train it with: python ml/train.py",
            details={"reason": str(exc)},
        ) from exc


current_user, optional_user, require_roles = build_auth_dependencies(
    settings.jwt_secret_key, settings.jwt_algorithm
)


def model_ready() -> bool:
    """Readiness depends on the model, because without it the service can do
    nothing useful -- unlike a cache, this dependency is not optional."""
    try:
        _forecaster()
        return True
    except Exception:
        return False
