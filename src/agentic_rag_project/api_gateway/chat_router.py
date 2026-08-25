"""`POST /chat/query` FastAPI route (T3 wire-up).

DESIGN 4.2 / TASK T3 assembly: this is the HTTP shell around
`ChatService.handle`. All business logic lives in `chat_service.py`;
this module only:
  * Parses the request (Pydantic does that for us via `Body`).
  * Resolves auth + workspace (via existing dependencies).
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
    get_current_user,
)
from agentic_rag_project.chat import (
    ChatQueryRequest,
    ChatQueryResponse,
    ChatService,
    ChatServiceError,
    EmptyQueryError,
)
from agentic_rag_project.db.session import get_db
from agentic_rag_project.observability.chat_metrics import (
    inc_chat_active_sessions,
    inc_long_context_fallback,
    inc_post_processor_redactions,
    record_chat_request,
)
from agentic_rag_project.observability.registry import get_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _resolve_workspace_id(
    ctx: UserContext, override: str | None
) -> uuid.UUID:
    """Pick the workspace for this request.

    `override` wins when the caller passes it explicitly (admin tooling,
    cross-workspace search). Otherwise the user's first workspace
    membership is used; if they're in none we 400.
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
) -> ChatQueryResponse:
    """Run one chat turn end-to-end (route → retrieve → answer → persist)."""
    workspace_id = _resolve_workspace_id(ctx, payload.workspace_id)
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
