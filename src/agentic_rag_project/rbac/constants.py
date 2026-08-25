"""Well-known UUIDs for system-scoped RBAC bindings (M6, revised 2026-08-25).

Why these constants exist
-------------------------
The original M6 plan (decision #5/#6) called for system-scoped roles
(`system_admin`) to bind via `UserRole.workspace_id = NULL`. That
conflicted with the baseline 0001 schema where `user_roles.workspace_id`
is part of the composite primary key `(user_id, role_id, workspace_id)`
AND `NOT NULL` — and `workspaces.owner_id` is itself `NOT NULL` FK to
`users.id`, so PG also rejects a "phantom" workspace.

Resolution: every system-scoped role binding uses a designated real
workspace row (`__system__`, owned by a placeholder user
`__system_owner__`) as its `workspace_id`. Semantics are unchanged from
the user perspective (system_admin grants cross-workspace permissions)
and the FK constraints stay satisfied.

Invariants
----------
1. These UUIDs are **stable forever**. Changing them would orphan every
   existing system-scoped binding and require a data migration.
2. The `__system__` workspace row is created by `seed_builtin_roles`
   with `status='enable'` so `assign_role()` accepts it; it is
   filtered out of the user's `workspace_ids` set by
   `api_gateway.dependencies._resolve_user_context` so the frontend
   workspace switcher never shows it.
3. The `__system_owner__` user has `status='disable'` and
   `is_super_admin=False`. Even if its placeholder hash leaked, login
   would be rejected by `_resolve_user_context`. Its only purpose is
   to satisfy the `workspaces.owner_id` FK — it carries no
   permissions and owns no other workspaces.
4. Frontend Page 12 (workspace list) and Page 11 (user user list) must
   filter out rows whose `name` starts with `__`. The convention is
   documented in this module and re-exported as `INTERNAL_NAME_PREFIX`.
"""

from __future__ import annotations

import uuid

# Designated "system" workspace. All `system_admin` (and any future
# system-scoped role) bindings reference this UUID via UserRole.workspace_id.
# Use uuid5(namespace, name) instead of zero-based hex literals — some
# UUID type impls (notably SQLAlchemy's `postgresql.UUID(as_uuid=True)`
# on SQLite, used by the in-memory test fixtures) collapse tiny
# integer-valued UUIDs like `00000000-...-0001` (whose `.int == 1`)
# down to bare ints and round-trip them as plain integers, which then
# crashes the UUID result processor. uuid5 produces a stable
# non-trivially-zero UUID that survives every storage path.
SYSTEM_WORKSPACE_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "system-workspace.rag.local"
)
SYSTEM_WORKSPACE_NAME: str = "__system__"

# Placeholder owner user for `__system__`. Required only because PG's FK
# `workspaces.owner_id REFERENCES users(id)` is NOT NULL. Not a real
# actor — `status='disable'`, `is_super_admin=False`, no email, no tokens.
SYSTEM_OWNER_USER_ID: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS, "system-owner.rag.local"
)
SYSTEM_OWNER_USERNAME: str = "__system_owner__"
SYSTEM_OWNER_EMAIL: str = "__system_owner@rag.local__"

# Convention: any DB row whose `name` starts with this prefix is an
# internal/placeholder row that must not be surfaced to end users.
# Documented for the frontend Page 12 (workspace list) and Page 11 (user
# list) filtering rules.
INTERNAL_NAME_PREFIX: str = "__"

__all__ = [
    "INTERNAL_NAME_PREFIX",
    "SYSTEM_OWNER_EMAIL",
    "SYSTEM_OWNER_USER_ID",
    "SYSTEM_OWNER_USERNAME",
    "SYSTEM_WORKSPACE_ID",
    "SYSTEM_WORKSPACE_NAME",
]