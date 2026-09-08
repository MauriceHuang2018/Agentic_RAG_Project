"""`/me/*` endpoints — operations scoped to the authenticated user.

Closed the T6.1 ↔ backend gap (M6): the frontend workspace switcher
(Chat page) needs the **real** workspace UUIDs the caller belongs to,
not a placeholder. Without this endpoint the store falls back to a
hard-coded `00000000-...-0000` sentinel, the chat request gets
rejected with 403 `not_a_member_of_workspace`, and the user sees
an empty/incorrect dropdown.

Endpoints (matches `docs/原型设计/前端页面规划与字段-表映射.md` §Page 14):
  - `GET /me/workspaces`    → list the caller's workspaces (id, name,
                              isolation_level, status), excluding the
                              designated `__system__` container.
  - `GET /me/permissions`   → flat set of `<resource>:<action>` keys
                              the caller holds across every workspace
                              binding, for the frontend `rbac.guard`
                              UX gate (see `stores/auth.ts`).

Both endpoints delegate to `UserContext` (already filtered for
`__system__` by `_resolve_user_context`). Super-admins see every
non-system workspace; their permission set is the wildcard `'*'`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_current_user,
)
from agentic_rag_project.db.models import Workspace
from agentic_rag_project.db.session import get_db

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/workspaces",
    summary="Workspaces the caller is a member of (for the workspace switcher).",
    response_model=list[dict],
)
def list_my_workspaces(
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    """Return the caller's workspaces, ready for `useWorkspaceStore.setFromLogin`.

    Rows are filtered by `UserContext.workspace_ids`, which is already
    `__system__`-free (see `api_gateway.dependencies._resolve_user_context`).
    Output is sorted by `name` (case-insensitive) so the UI dropdown
    is deterministic — Page 2's `el-select` doesn't enforce order on
    its own, and users get confused when the order flips between
    sessions.

    Response: `[{id, name, isolation_level, status}, ...]`
    """
    if not ctx.workspace_ids:
        return []
    rows = (
        session.execute(
            select(
                Workspace.id,
                Workspace.name,
                Workspace.isolation_level,
                Workspace.status,
            ).where(Workspace.id.in_(ctx.workspace_ids))
        )
        .all()
    )
    # Sort by name case-insensitive to keep the dropdown stable across
    # sessions (PG default collation is binary on citext, but we still
    # call .casefold() for SQLite tests + locale-friendly ordering).
    rows.sort(key=lambda r: r.name.casefold())
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "isolation_level": row.isolation_level,
            "status": row.status,
        }
        for row in rows
    ]


@router.get(
    "/permissions",
    summary="Permission keys the caller holds, for the frontend rbac.guard UX gate.",
    response_model=dict,
)
def get_my_permissions(
    ctx: Annotated[UserContext, Depends(get_current_user)],
) -> dict:
    """Return the caller's permission keys as `{permissions: list[str]}`.

    Mirrors `_resolve_user_context.permissions` so the frontend can
    populate `auth.permissions` after login. The authoritative check
    still lives in `dependencies.require_permission` — this endpoint
    is the UX gate only. The wildcard `'*'` is returned literally for
    super_admin to mirror `PERMISSION_WILDCARD` in
    `src/web/src/constants/permissions.ts`.

    The list is sorted alphabetically so the response is deterministic
    across calls; clients that need set semantics should dedupe on
    their side (the backend never returns duplicates — `permissions`
    is a `frozenset[str]`).
    """
    return {"permissions": sorted(ctx.permissions)}


__all__ = ["router"]