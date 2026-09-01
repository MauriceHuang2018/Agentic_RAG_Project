"""`POST /chat/query` FastAPI route (T3 wire-up).

DESIGN 4.2 / TASK T3 assembly: this is the HTTP shell around
`ChatService.handle`. All business logic lives in `chat_service.py`;
this module only:
  * Parses the request (Pydantic does that for us via `Body`).
  * Resolves auth + workspace (via existing dependencies).
  * Runs the M4.3 query guardrail (R13 — first line after
    workspace resolution; every LLM-bound input MUST pass through
    `QueryGuardrail.check()`).
  * Maps service errors → HTTP errors.
  * Commits the SQLAlchemy session.

The ChatService instance is built by the FastAPI app layer
(`api_gateway.dependencies`) using the real LiteLLM-backed
collaborators. Tests swap the dependency with a fake service.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_audit_service,
    get_current_user,
    get_query_guardrail,
)
from agentic_rag_project.audit import AuditEvent, AuditService
from agentic_rag_project.chat import (
    ChatQueryRequest,
    ChatQueryResponse,
    ChatService,
    ChatServiceError,
    EmptyQueryError,
)
from agentic_rag_project.db.models import Workspace
from agentic_rag_project.db.session import get_db
from agentic_rag_project.observability.chat_metrics import (
    inc_chat_active_sessions,
    inc_long_context_fallback,
    inc_post_processor_redactions,
    record_chat_request,
)
from agentic_rag_project.observability.registry import get_metrics
from agentic_rag_project.query_guardrail import QueryGuardrail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _resolve_workspace_id(
    ctx: UserContext,
    override: str | None,
    session: Session,
) -> uuid.UUID:
    """Pick the workspace for this request.

    `override` wins when the caller passes it explicitly (admin tooling,
    cross-workspace search). Otherwise the user's first workspace
    membership is used; if they're in none we 400.

    FK fallback (added 2026-09-01 for M6 /chat smoke): when the caller
    sends a `workspace_id` that does NOT exist in the `workspaces`
    table, we 400 instead of letting it flow into `ChatService.handle`,
    which would fail an `IntegrityError` on `INSERT Conversation`.
    The placeholder UUID `00000000-...` from the frontend's
    `workspace.ensureFallback()` (when `/me` does not return
    `workspaces[]`) would otherwise produce the same 500. Returning
    a clean 400 lets the UI surface "please pick a real workspace"
    instead of an opaque server error.
    """
    if override:
        try:
            wid = uuid.UUID(override)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid workspace_id: {override}"
            ) from exc
        if not ctx.is_super_admin and wid not in ctx.workspace_ids:
            raise HTTPException(
                status_code=403, detail="not a member of requested workspace"
            )
        # FK fallback: reject override UUID that doesn't exist in the
        # workspaces table. Runs AFTER the membership check so a
        # super_admin who sends a real-but-unknown UUID still gets the
        # same 400 (no special-case privilege for bogus ids).
        existing = session.get(Workspace, wid)
        if existing is None:
            raise HTTPException(
                status_code=400,
                detail=f"workspace not found: {wid}",
            )
        return wid
    if not ctx.workspace_ids:
        raise HTTPException(
            status_code=400, detail="user is not a member of any workspace"
        )
    return next(iter(ctx.workspace_ids))


@router.post(
    "/query",
    response_model=ChatQueryResponse,
    summary="Submit a chat query and receive the assistant answer",
)
def post_chat_query(
    payload: ChatQueryRequest,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[ChatService, Depends(_get_chat_service)],
    guardrail: Annotated[QueryGuardrail, Depends(get_query_guardrail)],
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
) -> ChatQueryResponse:
    """Run one chat turn end-to-end (route → retrieve → answer → persist).

    R13 — `QueryGuardrail.check()` runs as the first thing after
    workspace resolution. A blocked query records a
    `guardrail_block` audit event and returns 403; a clean query
    flows through to `ChatService.handle`.
    """
    workspace_id = _resolve_workspace_id(ctx, payload.workspace_id, session)
    # R13 — chat_router 入口第一行 QueryGuardrail.check
    guardrail_result = guardrail.check(payload.query or "")
    if not guardrail_result.allowed:
        audit_service.record(
            AuditEvent(
                user_id=str(ctx.user_id),
                action="guardrail_block",
                query=payload.query,
                sanitized_query=guardrail_result.sanitized_query,
                extra={
                    "category": guardrail_result.category,
                    "reason": guardrail_result.reason,
                    "matched_text": guardrail_result.matched_text,
                },
            )
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"guardrail_block: {guardrail_result.category} "
                f"({guardrail_result.reason})"
            ),
        )
    inc_chat_active_sessions(delta=1)
    router_class = "unknown"
    fallback_used = False
    status_label = "ok"
    started = time.perf_counter()
    try:
        response = service.handle(
            session=session,
            request=payload,
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            default_conversation_title=payload.query[:60],
        )
        # Map response → label values for the L1 histogram/counter.
        # We observe latency *once*, with the real labels, here.
        elapsed = time.perf_counter() - started
        router_class = response.route if response.route in {"direct", "agent", "long_context"} else "unknown"
        fallback_used = bool(response.fallback_triggered)
        get_metrics().chat_latency_seconds.labels(
            router_class=router_class,
            fallback_used="true" if fallback_used else "false",
        ).observe(elapsed)

        # Post-processor redactions.
        for rule_id, count in (response.redactions or {}).items():
            inc_post_processor_redactions(rule_id=str(rule_id), count=int(count))
        if response.fallback_triggered:
            inc_long_context_fallback(model="default")
    except EmptyQueryError as exc:
        status_label = "error"
        record_chat_request(
            workspace_id=workspace_id,
            router_class="unknown",
            fallback_used=False,
            status="error",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChatServiceError as exc:
        status_label = "error"
        logger.warning("chat service error: %s", exc)
        record_chat_request(
            workspace_id=workspace_id,
            router_class="unknown",
            fallback_used=False,
            status="error",
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        # Last-resort safety net. We log the traceback so on-call can
        # diagnose; the user sees a generic 500.
        status_label = "error"
        logger.exception("chat service unhandled error")
        record_chat_request(
            workspace_id=workspace_id,
            router_class="unknown",
            fallback_used=False,
            status="error",
        )
        raise HTTPException(
            status_code=500, detail="internal_error"
        ) from exc
    finally:
        inc_chat_active_sessions(delta=-1)

    record_chat_request(
        workspace_id=workspace_id,
        router_class=router_class,
        fallback_used=fallback_used,
        status=status_label,
    )
    session.commit()
    return response


# ---------------------------------------------------------------------------
# service dependency
# ---------------------------------------------------------------------------


# A module-level indirection so tests can monkey-patch it without
# having to patch the FastAPI dependency-injection machinery.
_chat_service_factory: Any = None


def set_chat_service_factory(factory: Any) -> None:
    """Override the service factory (called by the app layer / tests).

    `factory` must be a zero-arg callable returning a `ChatService`
    instance. The default returns a placeholder that raises — the
    real production wiring is in `api_gateway.app_factory` (T3 wire-up
    sister module) which calls this on startup.
    """
    global _chat_service_factory
    _chat_service_factory = factory


def _get_chat_service() -> ChatService:
    """FastAPI dependency — the factory is registered by the app layer."""
    if _chat_service_factory is None:
        raise HTTPException(
            status_code=503,
            detail="chat service is not configured (call set_chat_service_factory)",
        )
    return _chat_service_factory()


def get_router() -> APIRouter:
    """Mountable router (used by api_gateway.__init__)."""
    return router


__all__ = [
    "get_router",
    "post_chat_query",
    "router",
    "set_chat_service_factory",
]
