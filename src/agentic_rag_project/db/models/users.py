"""User and Workspace models.

User fields follow DESIGN 4.1:
  - id (UUID PK)
  - username / email (unique)
  - password_hash
  - status (enable / disable, default enable)
  - is_super_admin (bypasses workspace checks — system-level state flag)
  - attributes JSONB (reserved for phase-2 dynamic permissions)

M6 additions (DESIGN §4.2 + §4.6.2):
  - display_name / avatar_url — Page 14 self-service
  - deleted_at — soft delete (decision #9)

Workspace fields:
  - id (UUID PK)
  - name, owner_id (FK -> users.id)
  - status (enable / disable, default enable)
  - isolation_level (logical / physical)
  - config JSONB
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from agentic_rag_project.db.models.base import JSONB_TYPE as JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentic_rag_project.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from agentic_rag_project.db.models.documents import Document


class User(Base, TimestampMixin):
    """End-user account. Workspace membership is expressed via UserRole.

    `is_super_admin` is a system-level state field, not a role — it
    bypasses UserRole.workspace_id checks globally.

    `deleted_at` (M6) is the soft-delete marker: NULL means active,
    non-NULL means the user has been logically removed. Service-layer
    queries must filter `WHERE deleted_at IS NULL`.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # M6 Page 14: profile fields (nullable; older rows keep NULL)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enable"
    )  # 'enable' | 'disable'
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # M6 decision #9: soft delete (partial index `WHERE deleted_at IS NULL`)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Owned workspaces (workspaces.user -> User)
    owned_workspaces: Mapped[list["Workspace"]] = relationship(
        "Workspace",
        back_populates="owner",
        foreign_keys="Workspace.owner_id",
        cascade="all, delete-orphan",
    )
    # Documents this user uploaded
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="owner", foreign_keys="Document.owner_id"
    )


class Workspace(Base, TimestampMixin):
    """Tenant container. Documents belong to a workspace; users join via UserRole."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enable"
    )  # 'enable' | 'disable'
    isolation_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="logical"
    )  # 'logical' | 'physical'
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    owner: Mapped["User"] = relationship(
        "User", back_populates="owned_workspaces", foreign_keys=[owner_id]
    )
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="workspace"
    )


class ApiToken(Base):
    """Per-user API token for programmatic access (M6 / T5.7 / Page 14).

    The plaintext token is shown to the user ONCE at creation time;
    only `token_hash` (SHA-256 hex) and `token_prefix` (8 chars for UI
    identification) are persisted. The lookup in
    `api_gateway/dependencies.py::get_current_user` matches on
    `(token_prefix, token_hash)` and checks `revoked_at` /
    `expires_at` for liveness.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PasswordResetAudit(Base):
    """Audit trail for password resets (M6 / T1.3f / Page 14).

    Records every reset triggered by an admin (`admin_force`),
    the user themselves (`user_self`), or the system during seed
    (`system_seed`). The `(user_id, reset_at DESC)` index supports
    Page 14's "操作日志 Tab" 90-day query.
    """

    __tablename__ = "password_reset_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    reset_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reset_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="admin_force"
    )  # 'admin_force' | 'user_self' | 'system_seed'
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )