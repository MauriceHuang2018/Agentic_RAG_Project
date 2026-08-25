"""Status guard for users / workspaces / roles.

DESIGN T5.2:
- users / workspaces / roles share the same `status` semantics: 'enable' or
  'disable', default 'enable'.
- Setting `status='disable'` on a `Role` whose `is_system=True` is rejected —
  built-in roles must always be active.
- Login / list filters and role assignment use these helpers so the
  invariant lives in one place.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import Role, User, Workspace

logger = logging.getLogger(__name__)

ALLOWED_STATUSES: frozenset[str] = frozenset({"enable", "disable"})


class StatusError(Exception):
    """Raised when a status transition is invalid."""


def assert_can_change_status(entity: object, new_status: str) -> None:
    """Validate a status transition.

    Built-in roles (`is_system=True`) cannot be disabled. Any other entity
    with a `status` field accepts either of the two allowed values.
    """
    if new_status not in ALLOWED_STATUSES:
        raise StatusError(
            f"invalid status '{new_status}'; expected one of {sorted(ALLOWED_STATUSES)}"
        )
    if isinstance(entity, Role) and entity.is_system and new_status == "disable":
        raise StatusError("cannot disable a built-in role (is_system=True)")


def set_user_status(session: Session, user_id: uuid.UUID, new_status: str) -> User:
    """Flip a user's status with the standard guard."""
    user = session.get(User, user_id)
    if user is None:
        raise StatusError(f"user {user_id} not found")
    if new_status not in ALLOWED_STATUSES:
        raise StatusError(f"invalid status '{new_status}'")
    user.status = new_status
    session.commit()
    session.refresh(user)
    return user


def set_workspace_status(
    session: Session, workspace_id: uuid.UUID, new_status: str
) -> Workspace:
    """Flip a workspace's status."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise StatusError(f"workspace {workspace_id} not found")
    if new_status not in ALLOWED_STATUSES:
        raise StatusError(f"invalid status '{new_status}'")
    workspace.status = new_status
    session.commit()
    session.refresh(workspace)
    return workspace


def enabled_workspace_ids(session: Session) -> list[uuid.UUID]:
    """Return ids of enabled workspaces (for membership filters)."""
    rows = session.execute(
        select(Workspace.id).where(Workspace.status == "enable")
    ).scalars().all()
    return list(rows)


def enabled_roles(session: Session) -> list[Role]:
    """Return all enabled roles (filter applied at the DB layer)."""
    return list(
        session.execute(
            select(Role).where(Role.status == "enable").order_by(Role.name)
        ).scalars().all()
    )


def filter_enabled_workspaces(
    workspace_ids: Iterable[uuid.UUID],
) -> list[uuid.UUID]:
    """Module-level helper that doesn't hit the DB; intended for in-memory tests."""
    return [w for w in workspace_ids]  # caller is responsible for status check