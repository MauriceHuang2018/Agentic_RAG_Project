"""Unit tests for the RBAC module.

These exercise the seed/CRUD/assignment logic against an in-memory SQLite
DB. They cover the key DESIGN invariants:

  * 5 built-in roles seeded with `is_system=True`
  * All permission keys seeded
  * Built-in role cannot be deleted or disabled
  * Custom role CRUD works
  * Disabled role cannot be assigned
  * Disabled workspace cannot be assigned
  * Duplicate (user, role, workspace) is idempotent
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.rbac import Permission, Role, RolePermission, UserRole
from agentic_rag_project.db.models.users import User, Workspace
from agentic_rag_project.rbac import (
    AssignmentError,
    RoleError,
    StatusError,
    assign_role,
    builtin_role_names,
    create_custom_role,
    delete_role,
    enabled_roles,
    get_role_permissions,
    list_roles,
    list_user_roles,
    seed_builtin_roles,
    seed_demo_user,
    set_user_status,
    set_workspace_status,
    unassign_role,
    update_role,
)
from agentic_rag_project.rbac.constants import (
    SYSTEM_OWNER_USER_ID,
    SYSTEM_WORKSPACE_ID,
)


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory SQLite + seeded built-in roles. Single shared connection."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    seed_builtin_roles(s)
    try:
        yield s
    finally:
        s.close()


def _make_workspace(s: Session, *, status: str = "enable") -> Workspace:
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4().hex[:6]}",
        status=status,
        owner_id=uuid.uuid4(),
    )
    s.add(ws)
    s.commit()
    s.refresh(ws)
    return ws


def _make_user(s: Session, *, status: str = "enable") -> User:
    u = User(
        id=uuid.uuid4(),
        username=f"u-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.c",
        password_hash="x",
        status=status,
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    return u


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


def test_seed_creates_six_builtin_roles(session: Session) -> None:
    names = builtin_role_names()
    # M6 (decision #5/#6): 5 workspace-scoped roles + 1 system-scoped
    # `system_admin`. `auditor` is deprecated but kept in the catalog
    # so the row's status can be force-set to 'disable'.
    assert names == (
        "workspace_admin",
        "kb_admin",
        "doc_owner",
        "chat_user",
        "auditor",
        "system_admin",
    )
    rows = session.query(Role).filter(Role.is_system.is_(True)).all()
    assert {r.name for r in rows} == set(names)


def test_seed_marks_builtin_roles_as_system(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "workspace_admin").one()
    assert role.is_system is True
    assert role.status == "enable"


def test_seed_creates_all_permission_keys(session: Session) -> None:
    count = session.query(Permission).count()
    # M6 baseline: 31 keys + 2 sensitive:* = 33 (≥ 33 expected).
    assert count >= 33


def test_seed_is_idempotent(session: Session) -> None:
    # Re-seed; nothing should double-insert.
    seed_builtin_roles(session)
    roles = session.query(Role).filter(Role.is_system.is_(True)).count()
    assert roles == 6


def test_auditor_role_is_disabled_after_seed(session: Session) -> None:
    # M6 decision #5: auditor row stays but status='disable'.
    seed_builtin_roles(session)
    auditor = session.query(Role).filter(Role.name == "auditor").one()
    assert auditor.status == "disable"
    assert auditor.is_system is True  # row preserved for historical FKs


def test_system_admin_role_is_system_scoped(session: Session) -> None:
    # M6 decision #5/#6 (revised 2026-08-25): `workspace_scoped=False`
    # signals that bindings of this role use the designated
    # `SYSTEM_WORKSPACE_ID` (a real workspace row), NOT NULL —
    # baseline schema's UserRole.workspace_id is NOT NULL.
    seed_builtin_roles(session)
    sys_admin = session.query(Role).filter(Role.name == "system_admin").one()
    assert sys_admin.workspace_scoped is False
    # Workspace-scoped roles default to True.
    ws_admin = session.query(Role).filter(Role.name == "workspace_admin").one()
    assert ws_admin.workspace_scoped is True


def test_system_workspace_exists_after_seed(session: Session) -> None:
    # Seed creates the designated system workspace so system-scoped
    # bindings can reference it. Without this row, `seed_demo_user`
    # cannot create the `system_admin` binding (FK constraint).
    seed_builtin_roles(session)
    ws = session.get(Workspace, SYSTEM_WORKSPACE_ID)
    assert ws is not None
    assert ws.name == "__system__"
    assert ws.status == "enable"
    assert ws.owner_id == SYSTEM_OWNER_USER_ID


def test_system_owner_user_exists_after_seed(session: Session) -> None:
    # Placeholder owner required by `workspaces.owner_id` FK.
    # `status='disable'` refuses login so the placeholder hash can't
    # be misused even if it leaked.
    seed_builtin_roles(session)
    owner = session.get(User, SYSTEM_OWNER_USER_ID)
    assert owner is not None
    assert owner.username == "__system_owner__"
    assert owner.status == "disable"
    assert owner.is_super_admin is False


def test_seed_demo_user_binds_to_system_workspace(session: Session) -> None:
    # M6 revised (2026-08-25): binding uses SYSTEM_WORKSPACE_ID,
    # not NULL (was the original plan but conflicts with NOT NULL).
    seed_demo_user(session)
    binding = (
        session.query(UserRole)
        .join(User, User.id == UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(User.username == "demo_sys", Role.name == "system_admin")
        .one()
    )
    assert binding.workspace_id == SYSTEM_WORKSPACE_ID


def test_resolve_user_context_excludes_system_workspace(session: Session) -> None:
    # `__system__` is the binding container for system-scoped roles;
    # it must not appear in `UserContext.workspace_ids` (frontend
    # workspace switcher would surface a ghost entry otherwise).
    # Permissions granted via system-scoped bindings ARE still
    # collected — only the workspace is filtered.
    from agentic_rag_project.api_gateway.dependencies import (
        _resolve_user_context,
    )

    seed_builtin_roles(session)
    seed_demo_user(session)
    demo = session.query(User).filter(User.username == "demo_sys").one()
    real_ws = _make_workspace(session)
    chat_user = session.query(Role).filter(Role.name == "chat_user").one()
    assign_role(
        session,
        user_id=demo.id,
        role_id=chat_user.id,
        workspace_id=real_ws.id,
    )
    ctx = _resolve_user_context(session, demo.id)
    assert real_ws.id in ctx.workspace_ids
    assert SYSTEM_WORKSPACE_ID not in ctx.workspace_ids
    # Permissions from system_admin (audit:read, sensitive:*) still
    # present even though the workspace is filtered out.
    assert "audit:read" in ctx.permissions
    assert "sensitive:read" in ctx.permissions
    assert "sensitive:update" in ctx.permissions
    # Permission from chat_user (in real workspace) is also collected.
    assert "chat:ask" in ctx.permissions


def test_builtin_role_has_expected_permission_keys(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "workspace_admin").one()
    keys = get_role_permissions(session, role.id)
    assert "workspace:read" in keys
    assert "doc:upload" in keys
    assert "audit:read" in keys


# ---------------------------------------------------------------------------
# custom role CRUD
# ---------------------------------------------------------------------------


def test_create_custom_role(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="Read-only analytics",
        permission_keys=["doc:read", "evaluation:read"],
    )
    assert role.id is not None
    assert role.is_system is False
    assert role.status == "enable"
    keys = get_role_permissions(session, role.id)
    assert sorted(keys) == ["doc:read", "evaluation:read"]


def test_create_custom_role_rejects_builtin_name(session: Session) -> None:
    with pytest.raises(RoleError):
        create_custom_role(
            session,
            name="workspace_admin",
            description="duplicate",
            permission_keys=["doc:read"],
        )


def test_create_custom_role_rejects_duplicate_name(session: Session) -> None:
    create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    with pytest.raises(RoleError):
        create_custom_role(
            session,
            name="custom_analyst",
            description="",
            permission_keys=["doc:read"],
        )


def test_update_custom_role_metadata(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(session, role_id=role.id, description="updated")
    fresh = session.get(Role, role.id)
    assert fresh is not None
    assert fresh.description == "updated"


def test_update_role_replace_permissions(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(
        session,
        role_id=role.id,
        permission_keys=["doc:read", "evaluation:read"],
    )
    keys = get_role_permissions(session, role.id)
    assert sorted(keys) == ["doc:read", "evaluation:read"]


def test_cannot_rename_builtin_role(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "auditor").one()
    with pytest.raises(RoleError):
        update_role(session, role_id=role.id, name="audit_v2")


def test_cannot_change_builtin_role_permissions(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "auditor").one()
    with pytest.raises(RoleError):
        update_role(
            session,
            role_id=role.id,
            permission_keys=["doc:read"],
        )


def test_delete_custom_role(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    delete_role(session, role_id=role.id)
    assert session.get(Role, role.id) is None


def test_cannot_delete_builtin_role(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "chat_user").one()
    with pytest.raises(RoleError):
        delete_role(session, role_id=role.id)


# ---------------------------------------------------------------------------
# status guards
# ---------------------------------------------------------------------------


def test_can_disable_custom_role(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(session, role_id=role.id, status="disable")
    fresh = session.get(Role, role.id)
    assert fresh is not None
    assert fresh.status == "disable"


def test_cannot_disable_builtin_role(session: Session) -> None:
    role = session.query(Role).filter(Role.name == "workspace_admin").one()
    with pytest.raises(StatusError):
        update_role(session, role_id=role.id, status="disable")


def test_invalid_status_rejected(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    with pytest.raises(StatusError):
        update_role(session, role_id=role.id, status="archived")


def test_set_user_status(session: Session) -> None:
    user = _make_user(session)
    # status.py expects a uuid.UUID; passing a raw string would break the
    # SQLAlchemy UUID processor on SQLite (String fallback column).
    set_user_status(session, user.id, "disable")
    fresh = session.get(User, user.id)
    assert fresh is not None
    assert fresh.status == "disable"


def test_set_workspace_status(session: Session) -> None:
    ws = _make_workspace(session)
    set_workspace_status(session, ws.id, "disable")
    fresh = session.get(Workspace, ws.id)
    assert fresh is not None
    assert fresh.status == "disable"


def test_enabled_roles_filters_disabled(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(session, role_id=role.id, status="disable")
    names = {r.name for r in enabled_roles(session)}
    assert "custom_analyst" not in names
    assert "chat_user" in names


def test_list_roles_excludes_disabled_by_default(session: Session) -> None:
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(session, role_id=role.id, status="disable")
    names = {r.name for r in list_roles(session)}
    assert "custom_analyst" not in names


# ---------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------


def test_assign_role_happy_path(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session)
    role = session.query(Role).filter(Role.name == "chat_user").one()
    binding = assign_role(
        session,
        user_id=user.id,
        role_id=role.id,
        workspace_id=ws.id,
    )
    assert binding.user_id == user.id
    assert binding.joined_at is not None


def test_assign_role_idempotent(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session)
    role = session.query(Role).filter(Role.name == "chat_user").one()
    a = assign_role(session, user_id=user.id, role_id=role.id, workspace_id=ws.id)
    b = assign_role(session, user_id=user.id, role_id=role.id, workspace_id=ws.id)
    # UserRole uses a composite PK (user_id, role_id, workspace_id) — there is
    # no surrogate `id`, so identity is checked by the key tuple.
    assert (a.user_id, a.role_id, a.workspace_id) == (
        b.user_id,
        b.role_id,
        b.workspace_id,
    )


def test_assign_disabled_role_rejected(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session)
    role = create_custom_role(
        session,
        name="custom_analyst",
        description="",
        permission_keys=["doc:read"],
    )
    update_role(session, role_id=role.id, status="disable")
    with pytest.raises(AssignmentError):
        assign_role(
            session,
            user_id=user.id,
            role_id=role.id,
            workspace_id=ws.id,
        )


def test_assign_to_disabled_workspace_rejected(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session, status="disable")
    role = session.query(Role).filter(Role.name == "chat_user").one()
    with pytest.raises(AssignmentError):
        assign_role(
            session,
            user_id=user.id,
            role_id=role.id,
            workspace_id=ws.id,
        )


def test_unassign_role_removes_binding(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session)
    role = session.query(Role).filter(Role.name == "chat_user").one()
    assign_role(session, user_id=user.id, role_id=role.id, workspace_id=ws.id)
    unassign_role(session, user_id=user.id, role_id=role.id, workspace_id=ws.id)
    bindings = list_user_roles(session, user.id)
    assert bindings == []


def test_unassign_role_is_idempotent(session: Session) -> None:
    user = _make_user(session)
    ws = _make_workspace(session)
    role = session.query(Role).filter(Role.name == "chat_user").one()
    # No prior assignment — should not raise.
    unassign_role(session, user_id=user.id, role_id=role.id, workspace_id=ws.id)