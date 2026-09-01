"""Built-in RBAC seeds — 6 roles + 33 permission keys.

DESIGN T5.2 + M6 additions (DESIGN §4.6.3):
- 5 built-in workspace-scoped roles: workspace_admin, kb_admin,
  doc_owner, chat_user, auditor (auditor deprecated, kept as row
  with status='disable' — see `_ensure_role`).
- 1 system-scoped role: system_admin (decision #5/#6, revised
  2026-08-25: binds to the designated `__system__` workspace
  rather than `workspace_id = NULL`, see `constants.py`).
- Permission keys use `<resource>:<action>` naming (31 baseline +
  2 new sensitive:* keys = 33 total).
- Built-in roles are marked `is_system=true` so they cannot be
  deleted; `is_system=true` forces `status='enable'` (enforced in
  `status.py`) — auditor is the exception (see `_ensure_role`).
- `workspace_scoped` controls whether UserRole needs a
  workspace_id (True) or may have NULL (False for system-scoped
  roles like system_admin). **Note**: even system-scoped roles
  carry a non-NULL workspace_id (== `SYSTEM_WORKSPACE_ID`); the
  flag is metadata only and documents intent, not a NULL
  discriminator.

This module is the only place that defines the seed set. Production
deployments call `seed_builtin_roles(session)` at startup; unit
tests call it directly inside an in-memory SQLite fixture.
`seed_demo_user(session)` is a companion that bootstraps a demo
system_admin account for Page 15 smoke tests.

`seed_demo_data(session)` (added 2026-09-01) bootstraps two demo
workspaces (`acme-hq`, `acme-rd`) + two demo users (`alice`,
`bob`) + their workspace bindings so /chat is verifiable end to
end without manually wiring every row. Inspired by the
`demo2/app.js` mock data (总公司 / 研发部). Passwords come from
`Settings.demo_alice_password` / `Settings.demo_bob_password`
(`.env` keys `DEMO_ALICE_PASSWORD` / `DEMO_BOB_PASSWORD`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    Workspace,
)
from agentic_rag_project.rbac.constants import (
    SYSTEM_OWNER_EMAIL,
    SYSTEM_OWNER_USER_ID,
    SYSTEM_OWNER_USERNAME,
    SYSTEM_WORKSPACE_ID,
    SYSTEM_WORKSPACE_NAME,
)


def _get_demo_settings_passwords() -> tuple[str, str]:
    """Read demo account passwords from Settings.

    Imported lazily so tests that build a Settings fixture with
    `.env` absent still get the documented defaults (`alice_pass` /
    `bob_pass`). Reading directly via `get_settings()` would force
    every test to either set env vars or accept an `.env` lookup.
    """
    from agentic_rag_project.config import get_settings

    s = get_settings()
    return s.demo_alice_password, s.demo_bob_password

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleSpec:
    """One built-in role + the permission keys it grants.

    `workspace_scoped` controls how UserRole binds this role:
      - True (default) — `UserRole.workspace_id NOT NULL`; the user
        must be a workspace member to receive this role.
      - False — `UserRole.workspace_id NULL`; the user gets the role
        across all workspaces (system-scoped, e.g. `system_admin`).
    """

    name: str
    description: str
    permission_keys: tuple[str, ...]
    workspace_scoped: bool = True


# -----------------------------------------------------------------------------
# Permission key registry
# -----------------------------------------------------------------------------
# resource:<action> or resource:sub_resource:<action>
# Keep alphabetical by resource, then action, for diff-friendliness.

PERMISSION_KEYS: tuple[str, ...] = (
    # workspace
    "workspace:read",
    "workspace:update",
    "workspace:member:invite",
    "workspace:member:remove",
    # role
    "role:read",
    "role:create",
    "role:update",
    "role:delete",
    "role:assign",
    "role:unassign",
    # user
    "user:read",
    "user:disable",
    # kb (knowledge base / document collection)
    "kb:create",
    "kb:update",
    "kb:delete",
    "kb:read",
    # document
    "doc:upload",
    "doc:read",
    "doc:update",
    "doc:delete",
    "doc:reindex",
    "doc:acl:grant",
    # chat
    "chat:ask",
    "chat:history:read",
    "chat:delete",
    # audit
    "audit:read",
    # feedback
    "feedback:submit",
    "feedback:read",
    "feedback:category:manage",
    # evaluation
    "evaluation:run",
    "evaluation:read",
    # sensitive info (M6 / Page 15 / decision #6)
    "sensitive:read",
    "sensitive:update",
)


# -----------------------------------------------------------------------------
# Built-in role catalog
# -----------------------------------------------------------------------------

BUILTIN_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(
        name="workspace_admin",
        description="Full control over a workspace: members, roles, settings.",
        permission_keys=(
            "workspace:read",
            "workspace:update",
            "workspace:member:invite",
            "workspace:member:remove",
            "role:read",
            "role:create",
            "role:update",
            "role:delete",
            "role:assign",
            "role:unassign",
            "user:read",
            "kb:create",
            "kb:update",
            "kb:delete",
            "kb:read",
            "doc:upload",
            "doc:read",
            "doc:update",
            "doc:delete",
            "doc:reindex",
            "doc:acl:grant",
            "chat:ask",
            "chat:history:read",
            "chat:delete",
            "audit:read",
            "feedback:read",
            "feedback:category:manage",
            "evaluation:read",
        ),
    ),
    RoleSpec(
        name="kb_admin",
        description="Manages knowledge bases and documents inside a workspace.",
        permission_keys=(
            "kb:create",
            "kb:update",
            "kb:delete",
            "kb:read",
            "doc:upload",
            "doc:read",
            "doc:update",
            "doc:delete",
            "doc:reindex",
            "doc:acl:grant",
            "chat:ask",
            "chat:history:read",
            "evaluation:read",
        ),
    ),
    RoleSpec(
        name="doc_owner",
        description="Read + comment on documents the user owns or has been granted.",
        permission_keys=(
            "doc:read",
            "chat:ask",
            "chat:history:read",
            "feedback:submit",
        ),
    ),
    RoleSpec(
        name="chat_user",
        description="Default end-user role: ask questions and submit feedback.",
        permission_keys=(
            "doc:read",
            "chat:ask",
            "chat:history:read",
            "feedback:submit",
        ),
    ),
    RoleSpec(
        # M6 decision #5/#6: auditor deprecated — row kept but
        # status='disable' is forced at the end of `_ensure_role`,
        # so any prior `UserRole` binding stops granting permissions
        # at the next permission resolution (status filter in
        # rbac/assignments.py).
        name="auditor",
        description=(
            "Deprecated read-only audit access. Replaced by system_admin "
            "for cross-workspace audit viewing."
        ),
        permission_keys=(
            "audit:read",
            "evaluation:read",
            "feedback:read",
        ),
    ),
    RoleSpec(
        # M6 decision #5/#6: system-scoped, no workspace binding,
        # grants cross-workspace audit read + sensitive info
        # maintenance (Page 15).
        name="system_admin",
        description=(
            "System-wide administrator — cross-workspace audit log access "
            "and sensitive value maintenance (Page 15). System-scoped: "
            "UserRole.workspace_id is NULL."
        ),
        permission_keys=(
            "audit:read",
            "sensitive:read",
            "sensitive:update",
        ),
        workspace_scoped=False,
    ),
)


# -----------------------------------------------------------------------------
# Demo user (M6 — for Page 15 smoke tests)
# -----------------------------------------------------------------------------

DEMO_USERNAME = "demo_sys"
DEMO_PASSWORD = "demo_pass"
DEMO_EMAIL = "demo_sys@example.local"


# Placeholder hash used for `__system_owner__`. The user is created with
# `status='disable'` so login is rejected before the hash is ever compared
# — this string is just a non-NOT-NULL placeholder satisfying the column.
_SYSTEM_OWNER_PASSWORD_HASH = "!locked:placeholder"


# -----------------------------------------------------------------------------
# Seed entrypoint
# -----------------------------------------------------------------------------


def _ensure_system_owner_user(session: Session) -> User:
    """Create the placeholder owner user for `__system__` workspace.

    Required because `workspaces.owner_id` is NOT NULL FK to users.id.
    The owner has `status='disable'` and `is_super_admin=False` — it can
    never log in and carries no RBAC privileges. Its only purpose is to
    satisfy the referential integrity constraint.
    """
    owner = session.get(User, SYSTEM_OWNER_USER_ID)
    if owner is not None:
        return owner
    owner = User(
        id=SYSTEM_OWNER_USER_ID,
        username=SYSTEM_OWNER_USERNAME,
        email=SYSTEM_OWNER_EMAIL,
        password_hash=_SYSTEM_OWNER_PASSWORD_HASH,
        status="disable",  # refuses login
        is_super_admin=False,
    )
    session.add(owner)
    session.flush()
    logger.info("seeded placeholder owner user %s", SYSTEM_OWNER_USERNAME)
    return owner


def _ensure_system_workspace(session: Session) -> Workspace:
    """Create the designated system workspace if missing.

    All `system_admin` (and future system-scoped role) bindings reference
    this workspace via `UserRole.workspace_id`. The workspace is filtered
    out of the user's `workspace_ids` set in
    `api_gateway.dependencies._resolve_user_context` so the frontend
    workspace switcher never shows it.

    Status is `enable` so `assign_role()` accepts new bindings here.
    Frontend Page 12 (workspace list) filters rows whose `name` starts
    with `__` (see `rbac.constants.INTERNAL_NAME_PREFIX`).
    """
    ws = session.get(Workspace, SYSTEM_WORKSPACE_ID)
    if ws is not None:
        return ws
    # Owner must exist before the workspace — create it first.
    _ensure_system_owner_user(session)
    ws = Workspace(
        id=SYSTEM_WORKSPACE_ID,
        name=SYSTEM_WORKSPACE_NAME,
        owner_id=SYSTEM_OWNER_USER_ID,
        status="enable",
        isolation_level="logical",
    )
    session.add(ws)
    session.flush()
    logger.info("seeded system workspace id=%s", SYSTEM_WORKSPACE_ID)
    return ws


def _ensure_permission(session: Session, key: str) -> Permission:
    """Look up a permission by key; create if missing."""
    perm = session.execute(
        select(Permission).where(Permission.key == key)
    ).scalar_one_or_none()
    if perm is not None:
        return perm
    resource = key.split(":", 1)[0] if ":" in key else key
    perm = Permission(key=key, resource_type=resource, description="")
    session.add(perm)
    session.flush()
    return perm


def _hash_demo_password(plain: str) -> str:
    """Bcrypt-hash the demo password using the project's own `bcrypt`
    package (matching the contract used by `api_gateway/router.py`).

    Truncates input to 72 bytes after UTF-8 encoding to match the
    API login path — bcrypt's internal cap is implementation-defined.
    """
    encoded = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def _ensure_role(session: Session, spec: RoleSpec) -> Role:
    """Look up a role by name; create if missing (idempotent).

    Special cases:
      - `auditor` is force-disabled every time seed runs (decision #5).
        The row remains so historical UserRole bindings stay valid;
        `rbac.assignments.enabled_roles` already filters on
        `roles.status='enable'`, so disabling takes effect without
        touching the user_roles table.
    """
    role = session.execute(
        select(Role).where(Role.name == spec.name)
    ).scalar_one_or_none()
    if role is None:
        role = Role(
            name=spec.name,
            description=spec.description,
            status="enable",
            is_system=True,  # built-in roles are protected
            workspace_scoped=spec.workspace_scoped,
        )
        session.add(role)
        session.flush()
    # M6 decision #5: deprecated role stays as a row but is forced
    # disable on every seed pass.
    if spec.name == "auditor" and role.status != "disable":
        role.status = "disable"
        session.flush()
    return role


def _ensure_role_permission(
    session: Session, role: Role, permission: Permission
) -> None:
    """Bind role -> permission if not already bound."""
    existing = session.execute(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission_id == permission.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        RolePermission(role_id=role.id, permission_id=permission.id)
    )


def seed_builtin_roles(session: Session) -> dict[str, uuid.UUID]:
    """Idempotently insert the built-in roles + their permission bindings.

    Also ensures the system owner user + `__system__` workspace exist
    (revised 2026-08-25; see `constants.py` for rationale).

    Returns a dict mapping role name -> role id. Safe to call repeatedly.
    """
    # 0. ensure system owner + system workspace exist before any role
    #    binding (system-scoped roles reference the system workspace).
    _ensure_system_workspace(session)
    # 1. ensure all permission keys exist
    perms: dict[str, Permission] = {
        key: _ensure_permission(session, key) for key in PERMISSION_KEYS
    }
    # 2. ensure all roles exist
    roles: dict[str, Role] = {spec.name: _ensure_role(session, spec) for spec in BUILTIN_ROLES}
    # 3. ensure role-permission edges
    for spec in BUILTIN_ROLES:
        role = roles[spec.name]
        for key in spec.permission_keys:
            _ensure_role_permission(session, role, perms[key])
    session.commit()
    logger.info(
        "seeded %d builtin roles + %d permissions",
        len(roles),
        len(perms),
    )
    return {name: role.id for name, role in roles.items()}


def builtin_role_names() -> tuple[str, ...]:
    """Return the list of built-in role names (for guard checks)."""
    return tuple(spec.name for spec in BUILTIN_ROLES)


def seed_demo_user(session: Session) -> User | None:
    """Bootstrap a demo `system_admin` account for Page 15 smoke tests.

    Idempotent: returns the existing user when one already exists.
    Creates:
      - one user (username=DEMO_USERNAME, password=DEMO_PASSWORD
        hashed via the project's `bcrypt` package, is_super_admin=False,
        status='enable', display_name='Demo System Admin')
      - one UserRole binding to the `system_admin` role with
        `workspace_id = SYSTEM_WORKSPACE_ID` (designated system
        container, not NULL — see `constants.py` for rationale).

    Multiple users can be bound to `system_admin` in this same
    workspace (e.g. `demo_sys`, `demo_sys2`, future audit bots) —
    the workspace is a shared "system membership" container.

    Returns the User row, or None if `system_admin` was not seeded
    yet (call `seed_builtin_roles` first).
    """
    # System_admin must exist before we bind to it.
    sys_admin = session.execute(
        select(Role).where(Role.name == "system_admin")
    ).scalar_one_or_none()
    if sys_admin is None:
        logger.warning(
            "system_admin role missing; call seed_builtin_roles() first"
        )
        return None

    existing = session.execute(
        select(User).where(User.username == DEMO_USERNAME)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(
        username=DEMO_USERNAME,
        email=DEMO_EMAIL,
        password_hash=_hash_demo_password(DEMO_PASSWORD),
        status="enable",
        is_super_admin=False,
        display_name="Demo System Admin",
    )
    session.add(user)
    session.flush()

    binding = UserRole(
        user_id=user.id,
        role_id=sys_admin.id,
        # System-scoped binding: workspace_id references the designated
        # `__system__` workspace (revised 2026-08-25 — was NULL in the
        # original M6 plan; baseline schema requires NOT NULL).
        workspace_id=SYSTEM_WORKSPACE_ID,
    )
    session.add(binding)
    session.commit()
    logger.info(
        "seeded demo user %s bound to system_admin in workspace %s",
        DEMO_USERNAME,
        SYSTEM_WORKSPACE_ID,
    )
    return user


# -----------------------------------------------------------------------------
# Demo workspace + user data (M6 — Page 2 chat smoke)
#
# Inspired by `demo2/app.js` mock data (Acme Corp / 总公司 / 研发部)
# but anchored to the backend ORM (`User` / `Workspace` / `UserRole`).
# Idempotent: every entity is keyed by a stable uuid5 UUID so a second
# seed pass is a no-op. Owner users are disabled placeholders — they
# exist only to satisfy `workspaces.owner_id` NOT NULL FK, same pattern
# as `__system_owner__` (see `rbac.constants`).
# -----------------------------------------------------------------------------


# uuid5 UUIDs (stable forever — same dodge used by `__system__` to keep
# SQLite's UUID type impl happy). DO NOT hand-edit; if any of these
# change, every UserRole binding pointing at them becomes orphan.
DEMO_ACME_HQ_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "demo-workspace.acme-hq.rag.local"
)
DEMO_ACME_HQ_OWNER_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "demo-owner.acme-hq.rag.local"
)
DEMO_ACME_HQ_OWNER_USERNAME: str = "__demo_acme_hq_owner__"
DEMO_ACME_HQ_OWNER_EMAIL: str = "__demo_acme_hq_owner@rag.local__"
DEMO_ACME_HQ_NAME: str = "Acme Corp · 总公司"  # 总公司

DEMO_ACME_RD_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "demo-workspace.acme-rd.rag.local"
)
DEMO_ACME_RD_OWNER_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "demo-owner.acme-rd.rag.local"
)
DEMO_ACME_RD_OWNER_USERNAME: str = "__demo_acme_rd_owner__"
DEMO_ACME_RD_OWNER_EMAIL: str = "__demo_acme_rd_owner@rag.local__"
DEMO_ACME_RD_NAME: str = "Acme Corp · 研发部"  # 研发部

DEMO_ALICE_USERNAME: str = "alice"
DEMO_ALICE_EMAIL: str = "alice@example.local"
DEMO_BOB_USERNAME: str = "bob"
DEMO_BOB_EMAIL: str = "bob@example.local"


def _ensure_demo_workspace_owner(
    session: Session,
    *,
    user_id: uuid.UUID,
    username: str,
    email: str,
) -> User:
    """Create a disabled placeholder owner for a demo workspace.

    Mirrors `__system_owner__`: `status='disable'` refuses login so
    the placeholder hash can't be misused if it leaks. The owner
    owns exactly one workspace and holds no permissions.
    """
    owner = session.get(User, user_id)
    if owner is not None:
        return owner
    owner = User(
        id=user_id,
        username=username,
        email=email,
        password_hash="!locked:placeholder",
        status="disable",
        is_super_admin=False,
    )
    session.add(owner)
    session.flush()
    logger.info("seeded placeholder owner %s", username)
    return owner


def _ensure_demo_workspace(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    owner_id: uuid.UUID,
) -> Workspace:
    """Create a demo workspace if missing (idempotent by uuid5 id)."""
    ws = session.get(Workspace, workspace_id)
    if ws is not None:
        return ws
    ws = Workspace(
        id=workspace_id,
        name=name,
        owner_id=owner_id,
        status="enable",
        isolation_level="logical",
    )
    session.add(ws)
    session.flush()
    logger.info("seeded demo workspace %s id=%s", name, workspace_id)
    return ws


def _ensure_demo_user(
    session: Session,
    *,
    username: str,
    email: str,
    password: str,
) -> User | None:
    """Create a demo user if missing. Returns the row, or None on
    pre-existing mismatch (caller logs and continues).
    """
    existing = session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        username=username,
        email=email,
        password_hash=_hash_demo_password(password),
        status="enable",
        is_super_admin=False,
        display_name=username.title(),
    )
    session.add(user)
    session.flush()
    logger.info("seeded demo user %s", username)
    return user


def _ensure_user_role(
    session: Session,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Bind user -> role in workspace if missing.

    The (user_id, role_id, workspace_id) composite is the primary key
    on `user_roles`, so a duplicate insert raises IntegrityError. Idempotent
    by lookup-then-insert.
    """
    existing = session.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            UserRole.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        UserRole(
            user_id=user_id,
            role_id=role_id,
            workspace_id=workspace_id,
        )
    )
    session.flush()


def seed_demo_data(session: Session) -> dict[str, uuid.UUID]:
    """Bootstrap two demo workspaces + two demo users + bindings.

    Layout (mirrors `demo2/app.js` mock data so /chat demos match the
    prototype's mental model):

      workspaces
        acme-hq (总公司)   owner=__demo_acme_hq_owner__ (disabled)
        acme-rd (研发部)   owner=__demo_acme_rd_owner__ (disabled)
      users
        alice (chat_user in acme-hq, chat_user in acme-rd)
        bob   (kb_admin  in acme-hq)
      placeholder owners (status=disable, no login)
        __demo_acme_hq_owner__
        __demo_acme_rd_owner__

    All rows are keyed by stable uuid5 UUIDs so re-running this
    function is a no-op. Demo passwords come from
    `Settings.demo_alice_password` / `Settings.demo_bob_password`
    (env keys `DEMO_ALICE_PASSWORD` / `DEMO_BOB_PASSWORD`) — see
    `.env` for the dev defaults.

    Returns a dict mapping logical key -> row id for tests that need
    to look rows up without re-querying.

    Configured role catalog (must call `seed_builtin_roles` first):
      - chat_user (alice in both workspaces)
      - kb_admin  (bob in acme-hq)
    """
    # 0. Built-in roles must exist before binding — fail loud if missing.
    required_roles = ("chat_user", "kb_admin")
    found_roles: dict[str, Role] = {}
    for name in required_roles:
        role = session.execute(
            select(Role).where(Role.name == name)
        ).scalar_one_or_none()
        if role is None:
            logger.warning(
                "role %s missing; call seed_builtin_roles() first", name
            )
            return {}
        found_roles[name] = role

    # 1. Placeholder owners (so workspaces.owner_id FK is satisfied).
    _ensure_demo_workspace_owner(
        session,
        user_id=DEMO_ACME_HQ_OWNER_ID,
        username=DEMO_ACME_HQ_OWNER_USERNAME,
        email=DEMO_ACME_HQ_OWNER_EMAIL,
    )
    _ensure_demo_workspace_owner(
        session,
        user_id=DEMO_ACME_RD_OWNER_ID,
        username=DEMO_ACME_RD_OWNER_USERNAME,
        email=DEMO_ACME_RD_OWNER_EMAIL,
    )

    # 2. Workspaces.
    hq = _ensure_demo_workspace(
        session,
        workspace_id=DEMO_ACME_HQ_ID,
        name=DEMO_ACME_HQ_NAME,
        owner_id=DEMO_ACME_HQ_OWNER_ID,
    )
    rd = _ensure_demo_workspace(
        session,
        workspace_id=DEMO_ACME_RD_ID,
        name=DEMO_ACME_RD_NAME,
        owner_id=DEMO_ACME_RD_OWNER_ID,
    )

    # 3. Users — passwords from settings.
    alice_pw, bob_pw = _get_demo_settings_passwords()
    alice = _ensure_demo_user(
        session,
        username=DEMO_ALICE_USERNAME,
        email=DEMO_ALICE_EMAIL,
        password=alice_pw,
    )
    bob = _ensure_demo_user(
        session,
        username=DEMO_BOB_USERNAME,
        email=DEMO_BOB_EMAIL,
        password=bob_pw,
    )
    if alice is None or bob is None:
        session.rollback()
        logger.warning("demo user creation returned None; aborting seed")
        return {}

    # 4. Bindings.
    _ensure_user_role(
        session,
        user_id=alice.id,
        role_id=found_roles["chat_user"].id,
        workspace_id=hq.id,
    )
    _ensure_user_role(
        session,
        user_id=alice.id,
        role_id=found_roles["chat_user"].id,
        workspace_id=rd.id,
    )
    _ensure_user_role(
        session,
        user_id=bob.id,
        role_id=found_roles["kb_admin"].id,
        workspace_id=hq.id,
    )

    session.commit()
    logger.info(
        "demo data seeded: 2 workspaces + 2 users + 3 user-role bindings"
    )
    return {
        "acme_hq_id": hq.id,
        "acme_rd_id": rd.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
    }