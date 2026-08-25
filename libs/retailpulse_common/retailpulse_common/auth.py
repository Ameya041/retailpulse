"""JWT issuing/verification, password hashing and role-based authorization.

Why JWT here: every service must independently answer "who is this caller?".
A shared session store would put a synchronous lookup (and a hard dependency)
in front of every request on every service. A signed token lets each service
verify locally with no network call.

The trade-off is real and worth stating in an interview: a JWT cannot be
revoked before it expires. That is why the token lifetime is short (60 minutes
by default). A production system would add a refresh token plus a deny-list of
revoked JTIs in Redis.

Authorization is enforced *server-side* on every protected route. The frontend
hiding a button is a UX detail, not a security control.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from pydantic import BaseModel

from retailpulse_common.errors import ForbiddenError, UnauthorizedError

# bcrypt: deliberately slow, salted per-password, and the cost factor can be
# raised as hardware improves. Never a plain SHA hash -- those are built to be
# fast, which is exactly wrong for passwords.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# auto_error=False so a missing header raises our own 401 envelope rather than
# FastAPI's default shape.
bearer_scheme = HTTPBearer(auto_error=False)


class Role(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"
    WAREHOUSE_OPERATOR = "WAREHOUSE_OPERATOR"


class TokenPayload(BaseModel):
    """Decoded, verified claims."""

    sub: str            # user_id
    email: str
    role: Role
    exp: datetime
    iat: datetime
    jti: str

    @property
    def user_id(self) -> uuid.UUID:
        return uuid.UUID(self.sub)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        # Malformed hash in the DB -- treat as a failed login, never a 500.
        return False


def create_access_token(
    *,
    user_id: uuid.UUID | str,
    email: str,
    role: Role | str,
    secret_key: str,
    algorithm: str = "HS256",
    expires_minutes: int = 60,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role.value if isinstance(role, Role) else str(role),
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
        "jti": str(uuid.uuid4()),
        "iss": "retailpulse",
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, secret_key, algorithm=algorithm)


def decode_access_token(
    token: str, *, secret_key: str, algorithm: str = "HS256"
) -> TokenPayload:
    try:
        claims = jwt.decode(
            token,
            secret_key,
            algorithms=[algorithm],
            issuer="retailpulse",
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        # Covers a wrong signature, wrong issuer, and tampered payloads. The
        # message stays vague on purpose -- detail here helps an attacker.
        raise UnauthorizedError("Access token is invalid.") from exc

    try:
        return TokenPayload(
            sub=claims["sub"],
            email=claims.get("email", ""),
            role=Role(claims["role"]),
            exp=datetime.fromtimestamp(claims["exp"], tz=UTC),
            iat=datetime.fromtimestamp(claims["iat"], tz=UTC),
            jti=claims.get("jti", ""),
        )
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Access token is missing required claims.") from exc


def build_auth_dependencies(secret_key: str, algorithm: str = "HS256"):
    """Build ``current_user`` / ``require_roles`` bound to this service's config.

    Returned as a factory rather than module-level singletons so tests can
    build dependencies against a throwaway secret.
    """

    def current_user(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ] = None,
    ) -> TokenPayload:
        if credentials is None or not credentials.credentials:
            raise UnauthorizedError("Authorization header with a bearer token is required.")
        payload = decode_access_token(
            credentials.credentials, secret_key=secret_key, algorithm=algorithm
        )
        # Stash for the access log so requests can be attributed to a user.
        request.state.user_id = payload.sub
        request.state.user_role = payload.role.value
        return payload

    def optional_user(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ] = None,
    ) -> TokenPayload | None:
        """For endpoints that are public but personalise when signed in."""
        if credentials is None or not credentials.credentials:
            return None
        try:
            return current_user(request, credentials)
        except UnauthorizedError:
            return None

    def require_roles(*roles: Role):
        allowed = set(roles)

        # NOTE: `Depends(current_user)` is passed as a default value rather than
        # inside `Annotated[...]`. This module uses `from __future__ import
        # annotations`, so annotations are strings that FastAPI resolves against
        # module globals -- and `current_user` is a local of this factory, so an
        # annotated form would fail to resolve and the parameter would silently
        # be treated as a query parameter.
        def dependency(
            user: TokenPayload = Depends(current_user),
        ) -> TokenPayload:
            if user.role not in allowed:
                # 403, not 404: the caller is authenticated, just not permitted.
                raise ForbiddenError(
                    "Your role is not permitted to perform this action.",
                    details={
                        "required_roles": sorted(r.value for r in allowed),
                        "your_role": user.role.value,
                    },
                )
            return user

        return dependency

    return current_user, optional_user, require_roles
