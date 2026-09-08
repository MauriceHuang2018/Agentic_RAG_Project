"""Admin endpoints (M4.3.1.c + M4.4). Currently exposes:

  * `GET /admin/audit-logs`        — paginate the WORM audit log
                                     (M4.3.1.c, perm `audit:read`).
  * `GET /admin/csat/summary`      — single-point CSAT for one workspace
                                     (M4.4, perm `csat:read`).
  * `GET /admin/csat/timeseries`   — time-bucketed CSAT (M4.4).
  * `GET /admin/csat/by-category`  — per-category breakdown (M4.4).

DESIGN 2.2 #11 + DESIGN §3 (M4.4): admins (or any holder of the
respective permission) can paginate the audit log and read
CSAT aggregations. Updates/deletes are deliberately not
exposed — the DB trigger would refuse them anyway.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from agentic_rag_project.audit import AuditEvent, AuditService
from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_audit_service,
    get_current_user,
    require_permission,
    resolve_target_workspace,
)
from agentic_rag_project.db.session import get_db
from agentic_rag_project.observability.csat_queries import (
    CSATByCategoryResponse,
    CSATSummaryResponse,
    CSATTimeseriesResponse,
    compute_csat_by_category,
    compute_csat_summary,
    compute_csat_timeseries,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# Mirrors `audit.events.AuditAction` — keep in sync.
# M5 (2026-08-27): T1 added three literals (`csat_read`, `role_bind`,
# `sensitive_word_update`). The list filter on `/admin/audit-logs`
# must accept them too, otherwise admins couldn't query the rows
# they're about to write.
_ALLOWED_ACTIONS = frozenset({
    "query",
    "ingest",
    "delete",
    "access_denied",
    "guardrail_block",
    "feedback_submit",
    "role_assign",
    "sensitive_update",
    "csat_read",
    "role_bind",
    "sensitive_word_update",
})


@router.get(
    "/audit-logs",
    summary="Paginate WORM audit logs (admin only)",
    dependencies=[Depends(require_permission("audit:read"))],
)
async def list_audit_logs(
    user_id: str | None = Query(
        None, description="Filter by actor user UUID"
    ),
    action: str | None = Query(
        None,
        description="Filter by action literal (one of 8 whitelisted values)",
    ),
    ts_from: datetime | None = Query(
        None, description="Inclusive lower bound on `ts` (ISO 8601)"
    ),
    ts_to: datetime | None = Query(
        None, description="Inclusive upper bound on `ts` (ISO 8601)"
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    audit_service: AuditService = Depends(get_audit_service),
) -> list[dict]:
    """Return audit rows newest-first.

    Raises 422 when `ts_from > ts_to`. Raises 403 via the dependency
    chain when the caller lacks `audit:read`.
    """
    if action is not None and action not in _ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"action must be one of {sorted(_ALLOWED_ACTIONS)}; "
                f"got {action!r}"
            ),
        )
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise HTTPException(
            status_code=422,
            detail="ts_from must be <= ts_to",
        )
    return audit_service.query(
        user_id=user_id,
        action=action,
        ts_from=ts_from,
        ts_to=ts_to,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# M4.4 — CSAT read API
# ---------------------------------------------------------------------------
#
# Three endpoints, all GET, all require `csat:read` permission
# (super-admins bypass via the wildcard). The `workspace_id`
# query param is REQUIRED — there's no "current user default"
# fallback here because CSAT is a workspace-scoped metric and
# returning multi-workspace aggregates would mislead the dashboard.
#
# `window_days` accepts {1, 7, 30}; FastAPI's `ge`/`le` validators
# raise 422 (FastAPI validation error) for any other value. We
# map that to 400 with `pattern` for clarity in the API contract.
#
# Behaviour parity note: with `window_days=7` these endpoints
# return the same numbers as the Prometheus `csat_score` gauge
# within ±0.01 (the gauge is cached up to 60s behind the API's
# direct DB read).


@router.get(
    "/csat/summary",
    response_model=CSATSummaryResponse,
    summary="CSAT single-point summary for one workspace.",
    dependencies=[Depends(require_permission("csat:read"))],
)
def get_csat_summary(
    workspace_id: Annotated[
        str, Query(..., description="Workspace UUID (must be a member)")
    ],
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
    # FastAPI 0.115+ rejects params with defaults AFTER params
    # without defaults. `audit_service` has no default, so it MUST
    # appear before `window_days` (which has `Query(7, ...) =
    # 7`). The M4.4 audit gotcha (memory: m4-4-csat-dashboard)
    # documented the same constraint for `permission` deps.
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
    window_days: int = Query(
        7, ge=1, le=30, description="Rolling window in days (1, 7, or 30).",
    ),
) -> CSATSummaryResponse:
    """Return like / dislike counts and the resulting CSAT score.

    Zero-data workspaces return `csat_score=0.0` (NOT 404):
    CSAT 0.0 is a legal state, the workspace just opened.
    """
    target = resolve_target_workspace(ctx, workspace_id)
    data = compute_csat_summary(
        session, workspace_id=target, window_days=window_days,
    )
    # M5 T5 — `csat_read` audit row. We deliberately do NOT store
    # `target_workspace_id` in `extra` (decision 9: super-admin
    # cross-workspace dashboards would explode audit volume). The
    # action literal alone is enough to reconstruct *that* a CSAT
    # read happened; the per-workspace numbers are in the response
    # itself and the query string.
    audit_service.record(
        AuditEvent(
            user_id=str(ctx.user_id),
            action="csat_read",
            extra={
                "endpoint": "summary",
                "window_days": window_days,
            },
        )
    )
    return CSATSummaryResponse(
        workspace_id=str(data.workspace_id),
        window_days=data.window_days,
        like_count=data.like_count,
        dislike_count=data.dislike_count,
        total=data.total,
        csat_score=data.csat_score,
        generated_at=datetime.now(timezone.utc),
    )


@router.get(
    "/csat/timeseries",
    response_model=CSATTimeseriesResponse,
    summary="CSAT time series (day/hour bucket) for one workspace.",
    dependencies=[Depends(require_permission("csat:read"))],
)
def get_csat_timeseries(
    workspace_id: Annotated[
        str, Query(..., description="Workspace UUID (must be a member)")
    ],
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
    # FastAPI 0.115+ requires no-default params BEFORE defaulted
    # ones. `audit_service` is a Depends (no default), so it MUST
    # precede `window_days` and `bucket`. See M4.4 audit memory.
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
    window_days: int = Query(
        7, ge=1, le=30, description="Rolling window in days (1, 7, or 30).",
    ),
    bucket: str = Query(
        "day",
        pattern=r"^(hour|day)$",
        description="Bucket size: 'day' (default) or 'hour'.",
    ),
) -> CSATTimeseriesResponse:
    """Return like / dislike counts bucketed by `bucket`.

    Empty windows return `points: []`. The `bucket` parameter
    is enforced by FastAPI's `pattern=r"^(hour|day)$"` — anything
    else gets a 422 validation error.
    """
    target = resolve_target_workspace(ctx, workspace_id)
    data = compute_csat_timeseries(
        session,
        workspace_id=target,
        window_days=window_days,
        bucket=bucket,  # type: ignore[arg-type]
    )
    # M5 T5 — see `get_csat_summary` for the rationale on what we
    # DO and DO NOT store in `extra`. Same policy here.
    audit_service.record(
        AuditEvent(
            user_id=str(ctx.user_id),
            action="csat_read",
            extra={
                "endpoint": "timeseries",
                "window_days": window_days,
                "bucket": bucket,
            },
        )
    )
    return CSATTimeseriesResponse(
        workspace_id=str(data.workspace_id),
        window_days=data.window_days,
        bucket=data.bucket,
        points=[
            {
                "ts": p.ts,
                "like_count": p.like_count,
                "dislike_count": p.dislike_count,
                "total": p.total,
                "csat_score": p.csat_score,
            }
            for p in data.points
        ],
        generated_at=datetime.now(timezone.utc),
    )


@router.get(
    "/csat/by-category",
    response_model=CSATByCategoryResponse,
    summary="CSAT breakdown per attribution category for one workspace.",
    dependencies=[Depends(require_permission("csat:read"))],
)
def get_csat_by_category(
    workspace_id: Annotated[
        str, Query(..., description="Workspace UUID (must be a member)")
    ],
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
    # FastAPI 0.115+ requires no-default params BEFORE defaulted
    # ones. `audit_service` is a Depends (no default), so it MUST
    # precede `window_days`. See M4.4 audit memory.
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
    window_days: int = Query(
        7, ge=1, le=30, description="Rolling window in days (1, 7, or 30).",
    ),
) -> CSATByCategoryResponse:
    """Return like / dislike counts per attribution category_key.

    Categories are sorted by `dislike_count` DESC (worst category
    leads). Categories with zero rows in the window are omitted.
    """
    target = resolve_target_workspace(ctx, workspace_id)
    data = compute_csat_by_category(
        session, workspace_id=target, window_days=window_days,
    )
    # M5 T5 — see `get_csat_summary` for the rationale on what we
    # DO and DO NOT store in `extra`. Same policy here.
    audit_service.record(
        AuditEvent(
            user_id=str(ctx.user_id),
            action="csat_read",
            extra={
                "endpoint": "by_category",
                "window_days": window_days,
            },
        )
    )
    return CSATByCategoryResponse(
        workspace_id=str(data.workspace_id),
        window_days=data.window_days,
        categories=[
            {
                "category_key": row.category_key,
                "like_count": row.like_count,
                "dislike_count": row.dislike_count,
                "total": row.total,
                "dislike_rate": row.dislike_rate,
            }
            for row in data.categories
        ],
        generated_at=datetime.now(timezone.utc),
    )


def get_router() -> APIRouter:
    """Mountable router (used by `api_gateway.__init__`)."""
    return router


__all__ = ["get_router", "router"]