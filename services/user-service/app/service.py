"""Authentication and user management logic."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.schemas import LoginRequest, RegisterRequest
from retailpulse_common.auth import (
    Role,
    create_access_token,
    hash_password,
    verify_password,
)
from retailpulse_common.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)

logger = logging.getLogger("user-service")


class AuditAction:
    USER_REGISTERED = "USER_REGISTERED"
    USER_LOGIN = "USER_LOGIN"
    USER_LOGIN_FAILED = "USER_LOGIN_FAILED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_REACTIVATED = "USER_REACTIVATED"


class UserService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def audit(
        self,
        action: str,
        entity_type: str,
        *,
        user_id: uuid.UUID | None = None,
        entity_id: str | None = None,
        ip_address: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Record an audited action on the caller's transaction.

        No flush here: the entry commits with the action it describes, so the
        two can never disagree.
        """
        self.session.add(
            AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                ip_address=ip_address,
                detail=detail,
            )
        )

    def _audit_rejection(
        self,
        *,
        reason: str,
        user_id: uuid.UUID | None = None,
        entity_id: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Record a rejected login and commit it immediately.

        A rejection is signalled by raising, and the request-scoped session
        rolls back on any exception -- which would discard the very audit
        record describing the rejection. Committing here means failed logins
        survive their own failure, which is the whole point of auditing them.

        Safe to commit at this point because nothing else has been written on
        this transaction: authentication reads, it does not mutate, until the
        credentials have already been accepted.
        """
        self.audit(
            AuditAction.USER_LOGIN_FAILED,
            "user",
            user_id=user_id,
            entity_id=entity_id,
            ip_address=ip_address,
            detail={"reason": reason},
        )
        self.session.commit()
        logger.warning(
            "login rejected", extra={"reason": reason, "entity_id": entity_id}
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, payload: RegisterRequest, *, ip_address: str | None = None) -> User:
        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip(),
            # Always CUSTOMER. Privilege is granted, never self-assigned.
            role=Role.CUSTOMER.value,
            is_active=True,
        )
        self.session.add(user)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(
                "An account with that email already exists.",
                details={"email": payload.email},
            ) from exc

        self.audit(
            AuditAction.USER_REGISTERED,
            "user",
            user_id=user.user_id,
            entity_id=str(user.user_id),
            ip_address=ip_address,
        )
        logger.info("user registered", extra={"user_id": str(user.user_id)})
        return user

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    def authenticate(
        self,
        payload: LoginRequest,
        *,
        secret_key: str,
        algorithm: str,
        expires_minutes: int,
        ip_address: str | None = None,
    ) -> tuple[User, str]:
        user = self.session.scalar(select(User).where(User.email == payload.email))

        # One message and one code path for "no such user" and "wrong password".
        # Distinguishing them turns the login form into an account enumeration
        # oracle. The password is verified even when the user is missing, using
        # a dummy hash, so the response time does not leak existence either.
        if user is None:
            _burn_timing()
            self._audit_rejection(
                reason="unknown_email", entity_id=payload.email, ip_address=ip_address
            )
            raise UnauthorizedError("Incorrect email or password.")

        if not verify_password(payload.password, user.password_hash):
            self._audit_rejection(
                reason="bad_password",
                user_id=user.user_id,
                entity_id=str(user.user_id),
                ip_address=ip_address,
            )
            raise UnauthorizedError("Incorrect email or password.")

        if not user.is_active:
            # Distinct from bad credentials: the caller proved who they are,
            # they are simply not allowed in.
            self._audit_rejection(
                reason="inactive",
                user_id=user.user_id,
                entity_id=str(user.user_id),
                ip_address=ip_address,
            )
            raise ForbiddenError("This account has been deactivated.")

        user.last_login_at = datetime.now(UTC)
        token = create_access_token(
            user_id=user.user_id,
            email=user.email,
            role=Role(user.role),
            secret_key=secret_key,
            algorithm=algorithm,
            expires_minutes=expires_minutes,
        )
        self.audit(
            AuditAction.USER_LOGIN,
            "user",
            user_id=user.user_id,
            entity_id=str(user.user_id),
            ip_address=ip_address,
        )
        logger.info("user login", extra={"user_id": str(user.user_id)})
        return user, token

    # ------------------------------------------------------------------
    # Lookups and administration
    # ------------------------------------------------------------------
    def get(self, user_id: uuid.UUID) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFoundError(
                f"User {user_id} was not found.", details={"user_id": str(user_id)}
            )
        return user

    def list_users(self, *, role: Role | None = None, limit: int = 100) -> list[User]:
        stmt = select(User).order_by(User.created_at.desc()).limit(limit)
        if role is not None:
            stmt = stmt.where(User.role == role.value)
        return list(self.session.scalars(stmt).all())

    def set_role(
        self,
        user_id: uuid.UUID,
        role: Role,
        *,
        actor_id: uuid.UUID,
        ip_address: str | None = None,
    ) -> User:
        user = self.get(user_id)
        previous = user.role
        if previous == role.value:
            raise ConflictError(
                f"User already has the {role.value} role.",
                details={"user_id": str(user_id), "role": role.value},
            )

        user.role = role.value
        self.session.flush()
        self.audit(
            AuditAction.USER_ROLE_CHANGED,
            "user",
            user_id=actor_id,
            entity_id=str(user_id),
            ip_address=ip_address,
            detail={"from": previous, "to": role.value},
        )
        logger.info(
            "role changed",
            extra={"user_id": str(user_id), "from": previous, "to": role.value},
        )
        return user

    def set_active(
        self,
        user_id: uuid.UUID,
        is_active: bool,
        *,
        actor_id: uuid.UUID,
        ip_address: str | None = None,
    ) -> User:
        if user_id == actor_id and not is_active:
            # Locking yourself out is almost never intended, and if the last
            # admin does it there is no in-app way back.
            raise ConflictError("You cannot deactivate your own account.")

        user = self.get(user_id)
        user.is_active = is_active
        self.session.flush()
        self.audit(
            AuditAction.USER_REACTIVATED if is_active else AuditAction.USER_DEACTIVATED,
            "user",
            user_id=actor_id,
            entity_id=str(user_id),
            ip_address=ip_address,
        )
        return user

    def recent_audit_logs(self, limit: int = 100) -> list[AuditLog]:
        return list(
            self.session.scalars(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            ).all()
        )

    # ------------------------------------------------------------------
    # Local bootstrap
    # ------------------------------------------------------------------
    def ensure_admin(self, email: str, password: str) -> User | None:
        """Create the bootstrap admin if it does not exist. Local/dev only."""
        existing = self.session.scalar(select(User).where(User.email == email.lower()))
        if existing is not None:
            return None
        admin = User(
            email=email.lower(),
            password_hash=hash_password(password),
            full_name="RetailPulse Admin",
            role=Role.ADMIN.value,
            is_active=True,
        )
        self.session.add(admin)
        self.session.flush()
        logger.warning("bootstrap admin created", extra={"email": admin.email})
        return admin


# A precomputed bcrypt hash of a value nobody will ever submit. Verifying
# against it makes the "unknown email" path cost roughly the same as the
# "wrong password" path, so response timing does not reveal which accounts
# exist.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.7Zu9Zc0qk1lE7ZzZ8Q0m0Q0m0Q0m0Qu"


def _burn_timing() -> None:
    verify_password("timing-equalisation", _DUMMY_HASH)
