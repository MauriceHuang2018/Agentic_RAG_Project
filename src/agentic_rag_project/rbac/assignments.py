"""User <-> Role binding helpers + soft-delete service (M6 / T5.2 / T5.8).

DESIGN T5.2:
- USER_ROLE.workspace_id is REQUIRED — it's what defines workspace membership.
- Assigning a disabled role is rejected.
- Assigning a role to a user in a disabled workspace is rejected.
- Re-assigning the same (user, role, workspace) tuple is idempotent.
- Unassign removes the binding; workspace membership itself disappears.

DESIGN §4.6.2 (M6 decision #9):
- `soft_delete_user()` flips `users.deleted_at = now()`; downstream
  service-layer queries must filter `WHERE deleted_at IS NULL`.
  No UI "restore" path — recovery is via Page 8 audit log + manual
  SQL (documented but not exposed).
- `assign_role()` refuses to bind a soft-deleted user; the JWT auth
  path (`api_gateway/dependencies.py::_resolve_user_context`) and
  login endpoint already block those users, but defending in depth
  here keeps the rule local to the RBAC module too.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import (
    Role,
    User,
    UserRole,
    Workspace,
)
from agentic_rag_project.audit import AuditEvent, AuditService

logger = logging.getLogger(__name__)


class AssignmentError(Exception):
    """Raised when a user-role binding is invalid."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def assign_role(
    session: Session,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    workspace_id: uuid.UUID,
    granted_by: uuid.UUID | None = None,
    # M5 T5 — when the caller (an admin route) supplies an audit
    # service, emit a `role_bind` row tied to the *actor* (the
    # admin), not the *subject* (the user receiving the role).
    # Without this, role-binding events would be missing from
    # Page 8 audit logs. Default None keeps seed scripts and tests
    # that call `assign_role()` directly working without
    # constructing an `AuditService` they don't need.
    audit_service: "AuditService | None" = None,
) -> UserRole:
    """Bind a user to a role within a workspace. Idempotent on duplicate.

    Refuses to bind a soft-deleted user (M6 §4.6.2) — the admin must
    restore the user first (via manual SQL — no UI restore button).
    """
    role = session.get(Role, role_id)
    if role is None:
        raise AssignmentError(f"role {role_id} not found")
    if role.status != "enable":
        raise AssignmentError(f"role '{role.name}' is disabled and cannot be assigned")
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise AssignmentError(f"workspace {workspace_id} not found")
    if workspace.status != "enable":
        raise AssignmentError(f"workspace '{workspace.name}' is disabled")

    # M6 §4.6.2: soft-deleted users must not get new role bindings.
    # `db.get` is fine here — we already touched the user from the
    # caller side, and the column is small.
    user = session.get(User, user_id)
    if user is None:
        raise AssignmentError(f"user {user_id} not found")
    if user.deleted_at is not None:
        raise AssignmentError(
            f"user '{user.username}' is soft-deleted and cannot receive new roles"
        )

    existing = session.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            UserRole.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    binding = UserRole(
        user_id=user_id,
        role_id=role_id,
        workspace_id=workspace_id,
        granted_by=granted_by,
        granted_at=_utcnow(),
        joined_at=_utcnow(),
        invited_by=granted_by,
    )
    session.add(binding)
    session.commit()
    session.refresh(binding)

    # M5 T5 — emit the `role_bind` audit row AFTER the binding
    # commits, so a failed commit doesn't leave an orphan audit
    # row. The audit row goes through `audit_service` (Redis buffer
    # + WORM trigger + DB CHECK) rather than the legacy
    # `role_assign` literal which is preserved for historical
    # back-compat but no longer used for new writes.
    if audit_service is not None and granted_by is not None:
        audit_service.record(
            AuditEvent(
                user_id=str(granted_by),
                action="role_bind",
                extra={
                    "subject_user_id": str(user_id),
                    "role_id": str(role_id),
                    "role_name": role.name,
                    "workspace_id": str(workspace_id),
                },
            )
        )

    return binding


def unassign_role(
    session: Session,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Remove a user-role binding. Idempotent (no error if missing)."""
    binding = session.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            UserRole.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if binding is None:
        return
    session.delete(binding)
    session.commit()


def list_user_roles(
    session: Session, user_id: uuid.UUID
) -> list[UserRole]:
    """Return all bindings for the user across workspaces.

    Callers that filter for *active* users must additionally check
    `users.deleted_at IS NULL`; this helper returns role bindings
    regardless of soft-delete state so admin audit views can still
    surface a deleted user's historical membership.
    """
    return list(
        session.execute(
            select(UserRole).where(UserRole.user_id == user_id)
        ).scalars().all()
    )


def list_workspace_members(
    session: Session, workspace_id: uuid.UUID
) -> list[UserRole]:
    """Return every user-role binding inside a workspace.

    Same caveat as `list_user_roles` — soft-delete filtering is the
    caller's responsibility (Page 11 user table hides deleted rows;
    Page 14 "我的工作空间" excludes deleted users).
    """
    return list(
        session.execute(
            select(UserRole).where(UserRole.workspace_id == workspace_id)
        ).scalars().all()
    )


# -----------------------------------------------------------------------------
# Soft-delete service (M6 decision #9)
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SoftDeleteResult:
    """Returned by `soft_delete_user` so the admin endpoint can show
    the new `deleted_at` in its 200 response."""

    user_id: uuid.UUID
    username: str
    deleted_at: datetime
    was_already_deleted: bool


def soft_delete_user(
    session: Session,
    *,
    user_id: uuid.UUID,
    deleted_by: uuid.UUID | None = None,
) -> SoftDeleteResult:
    """Mark a user as soft-deleted (M6 decision #9).

    Behaviour:
      - Idempotent: re-deleting an already-deleted user returns the
        existing `deleted_at` and `was_already_deleted=True` instead
        of raising — the admin endpoint can still respond 200.
      - Does NOT touch `UserRole` bindings. Historical role bindings
        stay in place so `audit_logs` and admin reports can still
        surface them; the live permission resolver in
        `api_gateway/dependencies.py` filters on `users.deleted_at
        IS NULL` so the user immediately loses access.
      - Does NOT log to `audit_logs` here — the admin endpoint owns
        that write so the actor + source IP land in one row.

    No restoration is exposed in the UI; recovery is via Page 8
    audit log + manual SQL.
    """
    user = session.get(User, user_id)
    if user is None:
        raise AssignmentError(f"user {user_id} not found")
    if user.deleted_at is not None:
        return SoftDeleteResult(
            user_id=user.id,
            username=user.username,
            deleted_at=user.deleted_at,
            was_already_deleted=True,
        )
    now = _utcnow()
    user.deleted_at = now
    session.commit()
    logger.info(
        "soft-deleted user %s (id=%s) by actor=%s",
        user.username,
        user.id,
        deleted_by,
    )
    return SoftDeleteResult(
        user_id=user.id,
        username=user.username,
        deleted_at=now,
        was_already_deleted=False,
    )


def list_active_workspace_members(
    session: Session, workspace_id: uuid.UUID
) -> list[UserRole]:
    """Convenience helper: workspace members excluding soft-deleted users.

    Use this from the Page 11 "成员列表" view so deleted users don't
    clutter the table. Page 8 audit views still use the raw
    `list_workspace_members` to surface historical bindings.
    """
    stmt = (
        select(UserRole)
        .join(User, User.id == UserRole.user_id)
        .where(
            UserRole.workspace_id == workspace_id,
            User.deleted_at.is_(None),
        )
    )
    return list(session.execute(stmt).scalars().all())


__all__ = [
    "AssignmentError",
    "SoftDeleteResult",
    "assign_role",
    "list_active_workspace_members",
    "list_user_roles",
    "list_workspace_members",
    "soft_delete_user",
    "unassign_role",
]