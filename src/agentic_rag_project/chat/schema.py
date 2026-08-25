"""Pydantic schemas for the chat endpoint (T3 assembly).

DESIGN 4.2 / TASK T3 — wire-up: every `POST /chat/query` carries a
`ChatQueryRequest`, returns a `ChatQueryResponse`. Both shapes are
declared here so the router and the service can share them without a
circular import.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# Shared chunk of evidence surfaced to the frontend, regardless of
# which path produced the answer (direct / agent / long-context).
class CitationItem(BaseModel):
    """One source-chunk reference attached to an assistant message."""

    chunk_id: str
    document_name: str
    page_no: int | None = None
    relevance_score: float = 0.0


class StepItem(BaseModel):
    """Audit-log row describing one agent-graph node invocation."""

    step_id: str
    iteration: int
    node: str
    action: str
    detail: str = ""
    duration_ms: int = 0


class ChatQueryRequest(BaseModel):
    """Body of `POST /chat/query`."""

    query: str = Field(..., description="User's natural-language question")
    conversation_id: str | None = Field(
        default=None,
        description="Existing conversation id; null → create a new one",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Override workspace scope; defaults to user's primary workspace",
    )
    acl_filter: dict[str, Any] | None = Field(
        default=None,
        description="Optional caller-supplied extra ACL clauses (T5.3)",
    )
    max_iterations: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Override the agent runner's max_iterations (1-20)",
    )

    # NOTE: query emptiness / length validation happens in
    # `ChatService.handle` (raises EmptyQueryError → HTTP 400). We
    # intentionally do NOT use a Pydantic `field_validator` here so the
    # service can produce the same exception type for both
    # whitespace-only and oversize inputs.


class ChatQueryResponse(BaseModel):
    """Body returned by `POST /chat/query`."""

    conversation_id: str
    message_id: str
    answer: str
    citations: list[CitationItem] = Field(default_factory=list)
    steps: list[StepItem] = Field(default_factory=list)
    route: str  # 'direct' | 'agent' | 'long_context'
    iterations: int = 0
    fallback_triggered: bool = False
    truncated_by_max_iter: bool = False
    refused: bool = False
    redactions: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ChatQueryRequest",
    "ChatQueryResponse",
    "CitationItem",
    "StepItem",
]
