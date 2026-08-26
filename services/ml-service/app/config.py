"""ML service configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from retailpulse_common.config import ServiceSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


class MLSettings(ServiceSettings):
    service_name: str = "ml-service"
    port: int = 8008

    # No database of its own: this service reads a model artifact and a history
    # snapshot, and owns no transactional state.
    db_name: str = "unused"

    model_path: Path = REPO_ROOT / "ml" / "artifacts" / "demand_forecaster_v1.joblib"
    history_path: Path = REPO_ROOT / "data" / "generated" / "sales.csv"


@lru_cache(maxsize=1)
def get_settings() -> MLSettings:
    return MLSettings()
