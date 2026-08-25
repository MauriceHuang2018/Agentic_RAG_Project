"""RBAC — role seeding, assignments, and status guards.

DESIGN T5.2: 6 built-in roles (M6: + `system_admin`, `auditor`
deprecated but row kept) + custom role CRUD + USER_ROLE binding +
enable/disable for users / workspaces / roles. Built-in roles
(`is_system=true`) cannot be deleted or disabled (auditor is the
exception — see `seed._ensure_role`).

M6 additions (DESIGN §4.6.2 / decision #9):
- `soft_delete_user()` flips `users.deleted_at`; live paths filter
  it out. No UI restore.
- `list_active_workspace_members()` for Page 11's member table.
- `seed_demo_user()` bootstraps a `system_admin` account for the
  Page 15 smoke tests.
"""

from __future__ import annotations

from agentic_rag_project.rbac.assignments import (
    AssignmentError,
    SoftDeleteResult,
    assign_role,
    list_active_workspace_members,
    list_user_roles,
    list_workspace_members,
    soft_delete_user,
    unassign_role,
)
from agentic_rag_project.rbac.roles import (
    RoleError,
    create_custom_role,
    delete_role,
    get_role_permissions,
    list_roles,
    update_role,
)
from agentic_rag_project.rbac.seed import (
    BUILTIN_ROLES,
    DEMO_PASSWORD,
    DEMO_USERNAME,
    PERMISSION_KEYS,
    builtin_role_names,
    seed_builtin_roles,
    seed_demo_user,
)
from agentic_rag_project.rbac.status import (
    ALLOWED_STATUSES,
    StatusError,
    assert_can_change_status,
    enabled_roles,
    enabled_workspace_ids,
    set_user_status,
    set_workspace_status,
)

__all__ = [
    "ALLOWED_STATUSES",
    "AssignmentError",
    "BUILTIN_ROLES",
    "DEMO_PASSWORD",
    "DEMO_USERNAME",
    "PERMISSION_KEYS",
    "RoleError",
    "SoftDeleteResult",
    "StatusError",
    "assert_can_change_status",
    "assign_role",
    "builtin_role_names",
    "create_custom_role",
    "delete_role",
    "enabled_roles",
    "enabled_workspace_ids",
    "get_role_permissions",
    "list_active_workspace_members",
    "list_roles",
    "list_user_roles",
    "list_workspace_members",
    "seed_builtin_roles",
    "seed_demo_user",
    "set_user_status",
    "set_workspace_status",
    "soft_delete_user",
    "unassign_role",
    "update_role",
]