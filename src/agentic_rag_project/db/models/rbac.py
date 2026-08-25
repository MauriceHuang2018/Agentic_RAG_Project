"""RBAC models — roles, permissions, role-permission and user-role bindings.

Key design decisions (DESIGN 4.2, M6 revised 2026-08-25):
  - ROLE is a pure permission template (NO workspace_id; reusable across ws)
  - USER_ROLE.workspace_id is REQUIRED — defines workspace membership.
    System-scoped roles (e.g. `system_admin`) use the designated
    `SYSTEM_WORKSPACE_ID` (see `rbac.constants`) instead of NULL —
    `workspaces.owner_id` is itself NOT NULL FK to users.id, so PG
    rejects a "phantom" workspace.
  - `joined_at` + `invited_by` (from merged WORKSPACE_MEMBER) live here
  - PERMISSION.key uses `<resource>:<action>` naming (e.g. `doc:read`)
  - ROLE.status (enable/disable, default enable); is_system=true forces enable
  - ROLE.workspace_scoped is metadata only — True means "a binding of
    this role is expected to use a real workspace id"; False means "the
    binding should reference SYSTEM_WORKSPACE_ID". Application code
    should consult this flag to pick the right workspace_id when
    creating bindings.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentic_rag_project.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from agentic_rag_project.db.models.users import User, Workspace


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(Base, TimestampMixin):
    """Pure permission template. Reusable across workspaces via UserRole."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enable"
    )  # 'enable' | 'disable'
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # built-in roles cannot be deleted
    # M6 (decision #5/#6, revised 2026-08-25):
    #   True  = workspace-scoped role — a binding uses a real workspace
    #           id (default; preserves historical semantics).
    #   False = system-scoped role — a binding should reference
    #           `SYSTEM_WORKSPACE_ID` (the designated `__system__`
    #           workspace, see `rbac.constants`). NOT NULL, because the
    #           baseline schema's UserRole.workspace_id is NOT NULL.
    # This column is metadata only; the application consults it when
    # building new bindings (e.g. admin UI should auto-pick
    # SYSTEM_WORKSPACE_ID when assigning a system-scoped role).
    workspace_scoped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    role_permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base):
    """Atomic permission point, e.g. `doc:read`, `admin:audit`, `kb:manage`."""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )  # '<resource>:<action>'
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )  # 'doc' | 'user' | 'workspace' | ...


class RolePermission(Base):
    """Many-to-many: roles <-> permissions."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_perm"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    role: Mapped["Role"] = relationship("Role", back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship("Permission")


class UserRole(Base):
    """User <-> Role binding scoped to a workspace (workspace membership).

    `workspace_id` is REQUIRED — having a role in a workspace is what
    makes a user a member of that workspace. System-scoped roles
    (e.g. `system_admin`, `roles.workspace_scoped = False`) reference
    the designated `SYSTEM_WORKSPACE_ID` (`__system__` workspace owned
    by the placeholder `__system_owner__` user) instead of NULL —
    baseline schema makes the column NOT NULL. The application layer
    filters this workspace out of `UserContext.workspace_ids` so the
    frontend workspace switcher never sees it.

    `joined_at` and `invited_by` were merged from the original
    WORKSPACE_MEMBER table.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "workspace_id", name="uq_user_role_ws"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Merged from WORKSPACE_MEMBER
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    role: Mapped["Role"] = relationship("Role", back_populates="user_roles")
    workspace: Mapped["Workspace"] = relationship("Workspace")