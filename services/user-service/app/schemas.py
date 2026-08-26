"""User service API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from retailpulse_common.auth import Role

# 8 characters is the practical floor. Length matters far more than forced
# symbol classes, which mostly drive users toward predictable substitutions.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]
    full_name: Annotated[str, Field(min_length=2, max_length=160)]
    # Self-registration always creates a CUSTOMER. Privileged roles are granted
    # by an existing admin through a separate endpoint -- accepting a `role`
    # here would let anyone register themselves as ADMIN.

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Password cannot be only whitespace.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    # password_hash is deliberately absent. Response models are the last line
    # of defence against leaking it.


class TokenResponse(BaseModel):
    access_token: str
    # Not a credential -- this is the OAuth 2.0 token type the client should
    # put in the Authorization header.
    token_type: str = "bearer"  # noqa: S105
    expires_in_seconds: int
    user: UserRead


class RoleUpdateRequest(BaseModel):
    role: Role


class UserStatusUpdateRequest(BaseModel):
    is_active: bool


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audit_id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    ip_address: str | None
    detail: dict | None
    created_at: datetime
