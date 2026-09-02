"""`/me/*` endpoints — operations scoped to the authenticated user.

Closed the T6.1 ↔ backend gap (M6): the frontend workspace switcher
(Chat page) needs the **real** workspace UUIDs the caller belongs to,
not a placeholder. Without this endpoint the store falls back to a
hard-coded `00000000-...-0000` sentinel, the chat request gets
rejected with 403 `not_a_member_of_workspace`, and the user sees
an empty/incorrect dropdown.

Endpoint (matches `docs/原型设计/前端页面规划与字段-表映射.md` §Page 14):
  - `GET /me/workspaces`  → list the caller's workspaces (id, name,
                            isolation_level, status), excluding the
                            designated `__system__` container.

The endpoint delegates to `UserContext.workspace_ids` (already
filtered for `__system__` by `_resolve_user_context`) and joins the
`workspaces` table only for display fields. Super-admins see every
non-system workspace; regular users see only those they have a role
binding in.
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


__all__ = ["router"]