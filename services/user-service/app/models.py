"""User and audit persistence models.

Security decisions worth defending:

* Only a bcrypt *hash* is stored. There is no column that could ever hold a
  plaintext password, and no code path that logs one.
* Email is stored lower-cased with a unique index. Treating `A@b.com` and
  `a@b.com` as different accounts is a real account-takeover vector during
  password reset flows.
* Users are deactivated, never deleted. Orders reference `customer_id`
  forever, so a hard delete would orphan order history.
* The audit log lives in this service's own database rather than a shared one.
  Each service audits what it owns; analytics aggregates across them later.
  A single shared audit table would be a cross-service write and the one place
  every service could corrupt each other's data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from retailpulse_common.db import Base

# JSONB on Postgres (indexable, binary), plain JSON on SQLite for the test
# suite. Same Python-side API either way.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Never the password itself. bcrypt output is 60 chars; the column is wider
    # so the hash algorithm can be upgraded without a migration.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="CUSTOMER")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('CUSTOMER', 'ADMIN', 'WAREHOUSE_OPERATOR')",
            name="ck_users_role_valid",
        ),
        # Cheap guard against a bug writing an empty hash and creating an
        # account nobody -- or anybody -- can log into.
        CheckConstraint("length(password_hash) > 20", name="ck_users_password_hash_present"),
        CheckConstraint("length(email) >= 3", name="ck_users_email_present"),
        Index("ix_users_role", "role"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} role={self.role}>"


class AuditLog(Base):
    """Append-only record of security-relevant and administrative actions.

    Written on the same transaction as the action it describes, so an audit
    entry cannot exist for an action that rolled back, and vice versa.
    """

    __tablename__ = "audit_logs"

    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Nullable: a failed login has no authenticated user yet, but is exactly
    # the kind of event worth recording.
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(45))  # IPv6-safe length
    detail: Mapped[dict | None] = mapped_column(JSONType)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action} {self.entity_type}>"
