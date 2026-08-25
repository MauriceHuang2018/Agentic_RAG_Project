"""Agent state and result shapes for the LangGraph workflow (T3.2).

DESIGN 6.2 — Agentic path: plan → retrieve → reflect → (loop or synthesize).
The state is intentionally narrow: every field is something the agent
either produced (plan, retrieved_context, reflection) or needs from
the caller (query, history, user_context). We never store raw LLM
messages; chat history is the caller's responsibility.

`AgentStep` is the audit-log row: one per graph node invocation,
capturing what happened at that step so a human can replay the
reasoning later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypedDict

from agentic_rag_project.retrieval_direct.search import SearchResult


class AgentRoute(str, Enum):
    """How the runner was invoked."""

    AGENT = "agent"
    DIRECT = "direct"


class ReflectDecision(str, Enum):
    """Verdict emitted by the reflect node."""

    CONTINUE = "continue"  # need another plan → retrieve → reflect cycle
    ANSWER = "answer"  # have enough context, synthesize now
    REWRITE = "rewrite"  # plan needs a different sub-query (treated like CONTINUE)


# Default cap. Configurable per runner instance; DESIGN/TASK say 5.
DEFAULT_MAX_ITERATIONS = 5


@dataclass
class AgentStep:
    """One node invocation, suitable for `agent_steps` audit log."""

    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    iteration: int = 0
    node: str = ""  # 'plan' | 'retrieve' | 'reflect' | 'synthesize'
    action: str = ""  # free-form description for the audit reader
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "iteration": self.iteration,
            "node": self.node,
            "action": self.action,
            "detail": self.detail,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
        }


class AgentState(TypedDict, total=False):
    """LangGraph state payload.

    `total=False` because most fields are populated by individual nodes
    rather than the caller. LangGraph tolerates missing keys; node
    functions must use `.get()` for safety.
    """

    # ---- caller inputs ----
    query: str
    conversation_id: str
    user_context: dict[str, Any]  # user_id, workspace_id, roles
    history: list[dict[str, Any]]  # HistoryTurn dicts (newest last)

    # ---- plan node output ----
    sub_queries: list[str]  # decomposed sub-questions
    current_sub_query_index: int

    # ---- retrieve node output ----
    retrieved_chunks: list[dict[str, Any]]  # serialized SearchResults
    retrieved_chunk_ids: list[str]  # dedup'd, ordered by appearance

    # ---- reflect node output ----
    reflection: str  # free-form reason emitted by the LLM
    reflect_decision: str  # 'continue' | 'answer' | 'rewrite'

    # ---- synthesize node output ----
    final_answer: str
    citations: list[dict[str, Any]]

    # ---- bookkeeping ----
    iteration: int
    max_iterations: int
    fallback_triggered: bool  # True if the latest retrieval had a low score
    steps: list[dict[str, Any]]  # serialized AgentSteps


@dataclass
class AgentResult:
    """What the runner returns to the caller (api_gateway/chat)."""

    answer: str
    citations: list[SearchResult]
    steps: list[AgentStep] = field(default_factory=list)
    iterations: int = 0
    fallback_triggered: bool = False
    truncated_by_max_iter: bool = False

    def step_dicts(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.steps]

    def citation_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "score": c.score,
                "content": c.content,
            }
            for c in self.citations
        ]


def make_initial_state(
    *,
    query: str,
    user_context: dict[str, Any],
    conversation_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> AgentState:
    """Build the starting AgentState for one user turn."""
    return AgentState(
        query=query,
        user_context=user_context,
        conversation_id=conversation_id or "",
        history=list(history or []),
        sub_queries=[],
        current_sub_query_index=0,
        retrieved_chunks=[],
        retrieved_chunk_ids=[],
        reflection="",
        reflect_decision="",
        final_answer="",
        citations=[],
        iteration=0,
        max_iterations=max_iterations,
        fallback_triggered=False,
        steps=[],
    )


def append_step(state: AgentState, step: AgentStep) -> None:
    """In-place append used by node functions."""
    state.setdefault("steps", []).append(step.to_dict())