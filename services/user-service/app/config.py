"""User service configuration."""

from __future__ import annotations

from functools import lru_cache

from retailpulse_common.config import ServiceSettings


class UserSettings(ServiceSettings):
    service_name: str = "user-service"
    db_name: str = "retailpulse_user"
    port: int = 8004

    # Seeded on first startup in local/dev so the stack is usable immediately.
    # Guarded so it can never run outside local -- see main.py.
    seed_admin_email: str = "admin@retailpulse.local"
    seed_admin_password: str = "ChangeMe123!"  # noqa: S105 - local bootstrap only


@lru_cache(maxsize=1)
def get_settings() -> UserSettings:
    return UserSettings()
