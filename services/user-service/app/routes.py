"""Authentication and user administration routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import current_user, get_db_session, require_roles
from app.schemas import (
    AuditLogRead,
    LoginRequest,
    RegisterRequest,
    RoleUpdateRequest,
    TokenResponse,
    UserRead,
    UserStatusUpdateRequest,
)
from app.service import UserService
from retailpulse_common.auth import Role, TokenPayload

auth_router = APIRouter(prefix="/auth", tags=["authentication"])
user_router = APIRouter(prefix="/users", tags=["users"])

settings = get_settings()

SessionDep = Annotated[Session, Depends(get_db_session)]
AuthedDep = Annotated[TokenPayload, Depends(current_user)]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP for the audit trail.

    X-Forwarded-For is only trustworthy when a proxy we control sets it. It is
    recorded for forensics, never used for an authorization decision.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


@auth_router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a customer account",
    responses={
        409: {"description": "Email already registered."},
        422: {"description": "Invalid email or password too short."},
    },
)
def register(payload: RegisterRequest, request: Request, session: SessionDep) -> UserRead:
    """Create a new account.

    Always creates a CUSTOMER; the request cannot choose its own role.
    Passwords are bcrypt-hashed and never stored or logged in plaintext.
    """
    user = UserService(session).register(payload, ip_address=_client_ip(request))
    return UserRead.model_validate(user)


@auth_router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a JWT",
    responses={
        401: {"description": "Incorrect email or password."},
        403: {"description": "Account deactivated."},
    },
)
def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    """Authenticate and receive a bearer token.

    Unknown emails and wrong passwords return the same 401 with the same
    message, so the endpoint cannot be used to enumerate accounts.
    """
    user, token = UserService(session).authenticate(
        payload,
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_access_token_expire_minutes,
        ip_address=_client_ip(request),
    )
    return TokenResponse(
        access_token=token,
        expires_in_seconds=settings.jwt_access_token_expire_minutes * 60,
        user=UserRead.model_validate(user),
    )


@user_router.get(
    "/me",
    response_model=UserRead,
    summary="Current authenticated user",
    responses={401: {"description": "Missing or invalid bearer token."}},
)
def me(user: AuthedDep, session: SessionDep) -> UserRead:
    """Resolve the caller's token to a live user record.

    Read from the database rather than trusting the token's claims alone: a
    role change or deactivation must take effect without waiting for the
    token to expire.
    """
    return UserRead.model_validate(UserService(session).get(user.user_id))


@user_router.get(
    "",
    response_model=list[UserRead],
    summary="List users",
    responses={403: {"description": "Requires ADMIN."}},
)
def list_users(
    session: SessionDep,
    _: AdminDep,
    role: Annotated[Role | None, Query(description="Filter by role.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[UserRead]:
    """Directory of accounts. Requires ADMIN."""
    return [UserRead.model_validate(u) for u in UserService(session).list_users(role=role, limit=limit)]


@user_router.get(
    "/audit-logs",
    response_model=list[AuditLogRead],
    summary="Recent audit trail",
    responses={403: {"description": "Requires ADMIN."}},
)
def audit_logs(
    session: SessionDep,
    _: AdminDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLogRead]:
    """Security and administrative events, newest first."""
    return [AuditLogRead.model_validate(a) for a in UserService(session).recent_audit_logs(limit)]


@user_router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Get a user by ID",
    responses={403: {"description": "Requires ADMIN or self."}, 404: {"description": "No such user."}},
)
def get_user(user_id: uuid.UUID, session: SessionDep, caller: AuthedDep) -> UserRead:
    """A customer may read their own record; admins may read anyone's."""
    if caller.role is not Role.ADMIN and caller.user_id != user_id:
        from retailpulse_common.errors import ForbiddenError

        raise ForbiddenError("You may only view your own account.")
    return UserRead.model_validate(UserService(session).get(user_id))


@user_router.patch(
    "/{user_id}/role",
    response_model=UserRead,
    summary="Change a user's role",
    responses={
        403: {"description": "Requires ADMIN."},
        404: {"description": "No such user."},
        409: {"description": "User already has that role."},
    },
)
def set_role(
    user_id: uuid.UUID,
    payload: RoleUpdateRequest,
    request: Request,
    session: SessionDep,
    admin: AdminDep,
) -> UserRead:
    """Grant or revoke privilege. Requires ADMIN and is always audited."""
    user = UserService(session).set_role(
        user_id, payload.role, actor_id=admin.user_id, ip_address=_client_ip(request)
    )
    return UserRead.model_validate(user)


@user_router.patch(
    "/{user_id}/status",
    response_model=UserRead,
    summary="Activate or deactivate a user",
    responses={
        403: {"description": "Requires ADMIN."},
        404: {"description": "No such user."},
        409: {"description": "Cannot deactivate your own account."},
    },
)
def set_status(
    user_id: uuid.UUID,
    payload: UserStatusUpdateRequest,
    request: Request,
    session: SessionDep,
    admin: AdminDep,
) -> UserRead:
    """Soft disable. Accounts are never deleted -- orders reference them."""
    user = UserService(session).set_active(
        user_id, payload.is_active, actor_id=admin.user_id, ip_address=_client_ip(request)
    )
    return UserRead.model_validate(user)
