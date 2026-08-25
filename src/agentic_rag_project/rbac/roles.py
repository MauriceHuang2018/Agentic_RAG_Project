"""Role CRUD operations.

DESIGN T5.2:
- Workspace admins and super-admins can create / update / delete custom roles.
- Built-in (is_system=true) roles cannot be deleted and cannot have their
  `status` set to 'disable' (enforced in `status.py`).
- Custom roles default to `status='enable'`.
- Role names are globally unique.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import Permission, Role, RolePermission
from agentic_rag_project.rbac.seed import builtin_role_names
from agentic_rag_project.rbac.status import assert_can_change_status

logger = logging.getLogger(__name__)


class RoleError(Exception):
    """Raised when a role operation is invalid."""


def create_custom_role(
    session: Session,
    *,
    name: str,
    description: str,
    permission_keys: list[str],
) -> Role:
    """Create a non-system role with the given permission bindings.

    Raises RoleError if `name` collides with an existing role or a built-in
    name. The role's `is_system` defaults to False.
    """
    if name in builtin_role_names():
        raise RoleError(f"'{name}' is a built-in role name and cannot be reused")
    existing = session.execute(
        select(Role).where(Role.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        raise RoleError(f"role '{name}' already exists")

    role = Role(
        name=name,
        description=description,
        status="enable",
        is_system=False,
    )
    session.add(role)
    session.flush()
    _bind_permissions(session, role, permission_keys)
    session.commit()
    session.refresh(role)
    return role


def update_role(
    session: Session,
    *,
    role_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    permission_keys: list[str] | None = None,
) -> Role:
    """Update mutable fields of a role. Built-in role name + status are locked."""
    role = session.get(Role, role_id)
    if role is None:
        raise RoleError(f"role {role_id} not found")

    if name is not None and name != role.name:
        if role.is_system:
            raise RoleError("cannot rename a built-in role")
        if name in builtin_role_names():
            raise RoleError(f"'{name}' is a built-in role name")
        existing = session.execute(
            select(Role).where(Role.name == name)
        ).scalar_one_or_none()
        if existing is not None and existing.id != role.id:
            raise RoleError(f"role '{name}' already exists")
        role.name = name

    if description is not None:
        role.description = description

    if status is not None and status != role.status:
        assert_can_change_status(role, status)
        role.status = status

    if permission_keys is not None:
        # Built-in roles keep their seed permissions; custom roles can be edited.
        if role.is_system:
            raise RoleError("cannot change permission bindings on a built-in role")
        _replace_permissions(session, role, permission_keys)

    session.commit()
    session.refresh(role)
    return role


def delete_role(session: Session, role_id: uuid.UUID) -> None:
    """Delete a role. Built-in roles are rejected."""
    role = session.get(Role, role_id)
    if role is None:
        raise RoleError(f"role {role_id} not found")
    if role.is_system:
        raise RoleError("cannot delete a built-in role")
    session.delete(role)
    session.commit()


def list_roles(session: Session, *, include_disabled: bool = False) -> list[Role]:
    """Return all roles, newest first."""
    stmt = select(Role).order_by(Role.is_system.desc(), Role.created_at.desc())
    if not include_disabled:
        stmt = stmt.where(Role.status == "enable")
    return list(session.execute(stmt).scalars().all())


def get_role_permissions(session: Session, role_id: uuid.UUID) -> list[str]:
    """Return the permission keys bound to a role."""
    stmt = (
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
        .order_by(Permission.key)
    )
    return list(session.execute(stmt).scalars().all())


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _bind_permissions(
    session: Session, role: Role, permission_keys: list[str]
) -> None:
    """Insert role_permissions edges for each key (creating perms if needed)."""
    for key in permission_keys:
        perm = session.execute(
            select(Permission).where(Permission.key == key)
        ).scalar_one_or_none()
        if perm is None:
            perm = Permission(key=key, resource_type=key.split(":", 1)[0], description="")
            session.add(perm)
            session.flush()
        edge = RolePermission(role_id=role.id, permission_id=perm.id)
        session.add(edge)


def _replace_permissions(
    session: Session, role: Role, permission_keys: list[str]
) -> None:
    """Remove existing edges and re-bind from scratch."""
    existing = (
        session.execute(
            select(RolePermission).where(RolePermission.role_id == role.id)
        )
        .scalars()
        .all()
    )
    for edge in existing:
        session.delete(edge)
    session.flush()
    _bind_permissions(session, role, permission_keys)