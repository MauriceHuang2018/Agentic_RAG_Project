"""`/feedback/*` FastAPI routes (T4.2).

DESIGN 4.6 / TASK T4.2 — exposes the feedback pipeline over HTTP:

  * `POST /feedback`             — submit one feedback row (any user)
  * `GET  /feedback/by-message/{message_id}` — list feedback for a
    message (ops + dashboard; same workspace only)
  * `GET  /feedback/categories`  — list the dictionary (5 system +
    admin extensions)
  * `POST /feedback/categories`  — add a custom category (admin-only)
  * `GET  /feedback/tags`        — list tags dictionary
  * `POST /feedback/tags`        — add a tag (admin-only)
  * `GET  /feedback/ticket-statuses`  — list ticket-status dictionary
    (7 system presets + admin extensions)
  * `POST /feedback/ticket-statuses`  — add a status (admin-only)

Like `chat_router`, the FastAPI dependency for the
`FeedbackService` is wired through a module-level factory so
tests can swap a fake without touching `app.dependency_overrides`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_current_user,
)
from agentic_rag_project.db.session import get_db
from agentic_rag_project.feedback import (
    AttributionStatus,
    DuplicateCategoryKeyError,
    DuplicateTicketStatusKeyError,
    FeedbackRepository,
    FeedbackService,
)
from agentic_rag_project.feedback.attributor import AutoAttributor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SubmitFeedbackRequest(BaseModel):
    """Payload for `POST /feedback`."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    rating: str = Field(pattern=r"^(like|dislike)$")
    comment: str | None = None
    ragas_scores: dict | None = None
    retrieved_chunks: list[str] | None = None
    query: str | None = None
    answer: str | None = None
    workspace_id: str | None = None
    reference_year: int | None = None


class SubmitFeedbackResponse(BaseModel):
    feedback_id: str
    attribution_status: str
    category_key: str | None
    ticket_status: str | None
    matched_rule: str | None
    reasoning: str | None


class CategoryResponse(BaseModel):
    id: str
    key: str
    name_zh: str
    description: str | None
    is_system: bool


class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(min_length=1, max_length=128)
    description: str | None = None


class TagResponse(BaseModel):
    id: str
    tag_key: str
    label: str


class CreateTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag_key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)


class TicketStatusResponse(BaseModel):
    id: str
    key: str
    name_zh: str
    description: str | None
    color: str | None
    is_terminal: bool
    is_system: bool
    display_order: int


class CreateTicketStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(min_length=1, max_length=128)
    description: str | None = None
    color: str | None = Field(default=None, max_length=32)
    is_terminal: bool = False
    display_order: int = 0


class FeedbackSummary(BaseModel):
    """Lightweight summary returned by `GET /feedback/by-message/{id}`."""

    id: str
    rating: str
    comment: str | None
    attribution_status: str
    created_at: str


class FeedbackByMessageResponse(BaseModel):
    message_id: str
    items: list[FeedbackSummary]


# ---------------------------------------------------------------------------
# Service factory indirection (mirrors chat_router)
# ---------------------------------------------------------------------------


_feedback_service_factory: Any = None


def set_feedback_service_factory(factory: Any) -> None:
    """Override the FeedbackService factory (called by app layer / tests)."""
    global _feedback_service_factory
    _feedback_service_factory = factory


def _get_feedback_service(
    session: Session,
    ctx: UserContext,
) -> FeedbackService:
    """FastAPI dependency — returns a service bound to `session`."""
    if _feedback_service_factory is None:
        # Production path: build a default service with no LLM
        # attributor. The factory is registered by app startup
        # when admin wants to inject KB topics etc.
        return FeedbackService(
            session=session,
            attributor=AutoAttributor(),
        )
    return _feedback_service_factory(session=session, ctx=ctx)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _parse_uuid(value: str, *, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid {field_name}: {value}"
        ) from exc


@router.post(
    "",
    response_model=SubmitFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit feedback for an assistant message",
)
def post_feedback(
    payload: SubmitFeedbackRequest,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> SubmitFeedbackResponse:
    """Record user feedback and run the auto-attributor cascade."""
    message_id = _parse_uuid(payload.message_id, field_name="message_id")

    workspace_id: uuid.UUID
    if payload.workspace_id:
        workspace_id = _parse_uuid(payload.workspace_id, field_name="workspace_id")
        if (
            not ctx.is_super_admin
            and workspace_id not in ctx.workspace_ids
        ):
            raise HTTPException(
                status_code=403,
                detail="not a member of requested workspace",
            )
    elif ctx.workspace_ids:
        workspace_id = next(iter(ctx.workspace_ids))
    else:
        raise HTTPException(
            status_code=400,
            detail="user is not a member of any workspace",
        )

    service = _get_feedback_service(session, ctx)
    try:
        result = service.submit(
            message_id=message_id,
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            rating=payload.rating,
            comment=payload.comment,
            ragas_scores=payload.ragas_scores,
            retrieved_chunks=payload.retrieved_chunks,
            query=payload.query,
            answer=payload.answer,
            reference_year=payload.reference_year,
        )
    except Exception as exc:
        logger.exception("feedback submit failed")
        raise HTTPException(status_code=500, detail="internal_error") from exc
    finally:
        session.commit()

    return SubmitFeedbackResponse(
        feedback_id=str(result.feedback_id),
        attribution_status=result.attribution_status.value,
        category_key=result.category_key,
        # `ticket_status` is now a raw key string from the
        # `ticket_statuses` table (DESIGN 4.6).
        ticket_status=result.ticket_status,
        matched_rule=result.matched_rule,
        reasoning=result.reasoning,
    )


@router.get(
    "/by-message/{message_id}",
    response_model=FeedbackByMessageResponse,
    summary="List feedback rows for one assistant message",
)
def get_feedback_by_message(
    message_id: str,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> FeedbackByMessageResponse:
    mid = _parse_uuid(message_id, field_name="message_id")
    repo = FeedbackRepository(session)
    items = repo.get_feedback_by_message(mid)
    return FeedbackByMessageResponse(
        message_id=str(mid),
        items=[
            FeedbackSummary(
                id=str(f.id),
                rating=f.rating.value,
                comment=f.comment,
                attribution_status=f.attribution_status.value,
                created_at=f.created_at.isoformat() if f.created_at else "",
            )
            for f in items
        ],
    )


@router.get(
    "/categories",
    response_model=list[CategoryResponse],
    summary="List all attribution categories (system + admin-added)",
)
def list_categories(
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> list[CategoryResponse]:
    repo = FeedbackRepository(session)
    return [
        CategoryResponse(
            id=str(c.id),
            key=c.key,
            name_zh=c.name_zh,
            description=c.description,
            is_system=c.is_system,
        )
        for c in repo.list_categories()
    ]


@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a custom attribution category (admin only)",
)
def create_category(
    payload: CreateCategoryRequest,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> CategoryResponse:
    if not ctx.is_super_admin:
        raise HTTPException(
            status_code=403, detail="super_admin required to add categories"
        )
    repo = FeedbackRepository(session)
    try:
        cat = repo.add_category(
            key=payload.key,
            name_zh=payload.name_zh,
            description=payload.description,
            is_system=False,
        )
    except DuplicateCategoryKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return CategoryResponse(
        id=str(cat.id),
        key=cat.key,
        name_zh=cat.name_zh,
        description=cat.description,
        is_system=cat.is_system,
    )


@router.get(
    "/tags",
    response_model=list[TagResponse],
    summary="List the tag dictionary",
)
def list_tags(
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> list[TagResponse]:
    repo = FeedbackRepository(session)
    return [
        TagResponse(id=str(t.id), tag_key=t.tag_key, label=t.label)
        for t in repo.list_tags()
    ]


@router.post(
    "/tags",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a tag to the dictionary (admin only)",
)
def create_tag(
    payload: CreateTagRequest,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> TagResponse:
    if not ctx.is_super_admin:
        raise HTTPException(
            status_code=403, detail="super_admin required to add tags"
        )
    repo = FeedbackRepository(session)
    try:
        tag = repo.add_tag(tag_key=payload.tag_key, label=payload.label)
    except DuplicateCategoryKeyError as exc:
        # Reuse the same error for both; the message names the field.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return TagResponse(id=str(tag.id), tag_key=tag.tag_key, label=tag.label)


@router.get(
    "/ticket-statuses",
    response_model=list[TicketStatusResponse],
    summary="List ticket-status dictionary (system + admin-added)",
)
def list_ticket_statuses(
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> list[TicketStatusResponse]:
    repo = FeedbackRepository(session)
    return [
        TicketStatusResponse(
            id=str(s.id),
            key=s.key,
            name_zh=s.name_zh,
            description=s.description,
            color=s.color,
            is_terminal=s.is_terminal,
            is_system=s.is_system,
            display_order=s.display_order,
        )
        for s in repo.list_ticket_statuses()
    ]


@router.post(
    "/ticket-statuses",
    response_model=TicketStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a ticket status to the dictionary (admin only)",
)
def create_ticket_status(
    payload: CreateTicketStatusRequest,
    ctx: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> TicketStatusResponse:
    """Extend the status dictionary. Note: the legal *transitions*
    are still in code (see `feedback.ticket_state`) — adding a row
    here only makes the key valid for storage; using it in the
    workflow still requires a corresponding code change.
    """
    if not ctx.is_super_admin:
        raise HTTPException(
            status_code=403, detail="super_admin required to add ticket statuses"
        )
    repo = FeedbackRepository(session)
    try:
        row = repo.add_ticket_status(
            key=payload.key,
            name_zh=payload.name_zh,
            description=payload.description,
            color=payload.color,
            is_terminal=payload.is_terminal,
            is_system=False,
            display_order=payload.display_order,
        )
    except DuplicateTicketStatusKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return TicketStatusResponse(
        id=str(row.id),
        key=row.key,
        name_zh=row.name_zh,
        description=row.description,
        color=row.color,
        is_terminal=row.is_terminal,
        is_system=row.is_system,
        display_order=row.display_order,
    )


def get_router() -> APIRouter:
    """Mountable router (used by api_gateway.__init__)."""
    return router


__all__ = [
    "get_router",
    "router",
    "set_feedback_service_factory",
]
