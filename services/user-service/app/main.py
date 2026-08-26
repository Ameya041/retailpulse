"""User service entrypoint."""

from __future__ import annotations

import logging

from app.config import get_settings
from app.deps import database_ready, get_database
from app.routes import auth_router, user_router
from app.service import UserService
from retailpulse_common.app import create_service_app

settings = get_settings()
logger = logging.getLogger("user-service")


def _seed_bootstrap_admin() -> None:
    """Create a starter admin so a fresh local stack is usable.

    Hard-gated to the local environment. Seeding a known-password admin into
    staging or production would be a straightforward backdoor, so this refuses
    to run anywhere else.
    """
    if settings.environment != "local":
        logger.info("bootstrap admin seeding skipped", extra={"environment": settings.environment})
        return
    try:
        with get_database().session() as session:
            UserService(session).ensure_admin(
                settings.seed_admin_email, settings.seed_admin_password
            )
    except Exception:
        # A database that is not up yet must not stop the process from
        # starting -- the readiness probe reports the real state.
        logger.exception("bootstrap admin seeding failed")


app = create_service_app(
    settings=settings,
    title="RetailPulse User Service",
    description=(
        "Accounts, authentication and role-based authorization.\n\n"
        "**Authentication.** Passwords are bcrypt-hashed (cost 12). Login returns a "
        "short-lived JWT that every other service verifies locally with no shared "
        "session store and no network call.\n\n"
        "**Authorization.** Three roles: CUSTOMER, ADMIN, WAREHOUSE_OPERATOR. Roles are "
        "granted by an admin and can never be self-assigned at registration.\n\n"
        "**Auditing.** Registrations, logins, failed logins and role changes are written "
        "to `audit_logs` on the same transaction as the action itself."
    ),
    checks={"database": database_ready},
    on_startup=_seed_bootstrap_admin,
)

app.include_router(auth_router)
app.include_router(user_router)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=settings.is_local)
