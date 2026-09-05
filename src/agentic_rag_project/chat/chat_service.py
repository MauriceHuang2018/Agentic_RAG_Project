"""Chat service — orchestrates the retrieval / agent / fallback paths.

DESIGN 4.2 / TASK T3 wire-up: this is the brain of `POST /chat/query`.
The router (`api_gateway.chat_router`) is a thin HTTP shell; every
real decision lives here. All collaborators are injected via the
constructor so unit tests can swap in fakes without monkey-patching
modules.

Flow per request:
  1. Load history (Redis sliding window) → format for prompt.
  2. Route the query (T3.1 ConfidenceRouter) → direct | agent.
  3. Execute the chosen path:
       * direct  → TwoStageSearcher + tiny prompt completion
       * agent   → AgentRunner.run (T3.2).  If the runner reports
                   `fallback_triggered=True` we pivot to
                   LongContextFallback (T3.3) on the same parents.
  4. Post-process: sensitive-word filter, then PII mask (T3.4).
  5. Persist: user Message, assistant Message, Citations (T2.4).

All five stages are independently mockable. Errors from collaborators
propagate as `ChatServiceError` so the router can map them to a
single HTTP 500.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from qdrant_client.http import models as qmodels

from agentic_rag_project.agent_core.runner import (
    AgentRunner,
    AgentRunnerError,
)
from agentic_rag_project.agent_core.state import (
    AgentResult,
    AgentState,
    AgentStep,
)
from agentic_rag_project.chat.schema import (
    ChatQueryRequest,
    ChatQueryResponse,
    CitationItem,
    StepItem,
)
from agentic_rag_project.db.models import (
    Chunk,
    Conversation,
    Document,
    Message,
)
from agentic_rag_project.post_processor.filter import SensitiveWordFilter
from agentic_rag_project.post_processor.mask import Masker
from agentic_rag_project.retrieval_direct.citations import record_citations
from agentic_rag_project.retrieval_direct.long_context_fallback import (
    LongContextFallback,
    LongContextFallbackError,
    LongContextResult,
)
from agentic_rag_project.retrieval_direct.memory import (
    ConversationMemory,
    HistoryTurn,
)
from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.two_stage import (
    SearcherLike,
    TwoStageResult,
    TwoStageSearcher,
)
from agentic_rag_project.router.classifier import (
    ConfidenceRouter,
    RouteDecision,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChatServiceError(Exception):
    """Wraps any collaborator failure so the router sees one exception type."""


class EmptyQueryError(ChatServiceError):
    """Validation surfaced as 400."""


# ---------------------------------------------------------------------------
# Collaborator Protocols (testability)
# ---------------------------------------------------------------------------


class LLMSynthesizer(Protocol):
    """Synthesizes a direct-path answer given context + query.

    The default production implementation will route through LiteLLM;
    tests substitute a deterministic lambda.
    """

    def synthesize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout: float,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Internal outcome bundles
# ---------------------------------------------------------------------------


@dataclass
class DirectPathOutcome:
    """Result of the single-shot retrieval path."""

    answer: str
    citations: list[CitationItem]
    route: str = "direct"
    fallback_triggered: bool = False


@dataclass
class AgentPathOutcome:
    """Result of the multi-hop agent path, before post-processing."""

    result: AgentResult
    citations: list[CitationItem]
    # When the agent runner reports `fallback_triggered=True` we ask
    # `LongContextFallback` to redo the answer with the full parent
    # bodies. The synthesized override is stored here so persistence
    # + response share the same data.
    long_context_override: LongContextResult | None = None


# ---------------------------------------------------------------------------
# ChatService
# ---------------------------------------------------------------------------


@dataclass
class ChatService:
    """Top-level orchestrator. Single instance can serve many requests."""

    searcher: SearcherLike
    two_stage: TwoStageSearcher
    router: ConfidenceRouter
    agent_runner: AgentRunner
    long_context: LongContextFallback
    memory: ConversationMemory
    sensitive_filter: SensitiveWordFilter
    masker: Masker
    # Direct-path synthesizer is required — wired by the FastAPI app
    # layer so the service has no global LiteLLM dependency.
    direct_synthesizer: LLMSynthesizer
    # Default knobs.
    default_max_iterations: int = 5
    direct_top_k: int = 5
    direct_answer_chars: int = 4000
    history_max_chars: int = 4000

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------

    def handle(
        self,
        *,
        session: Session,
        request: ChatQueryRequest,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        default_conversation_title: str = "",
        acl_filter: qmodels.Filter | None = None,
    ) -> ChatQueryResponse:
        """Process one `POST /chat/query` synchronously.

        Steps:
          1. Validate / normalize inputs.
          2. Ensure a Conversation exists.
          3. Persist the user Message.
          4. Load + format history.
          5. Route → run chosen path.
          6. Post-process (filter + mask).
          7. Persist assistant Message + Citations.
          8. Return the response.

        The caller (router) controls commit/rollback; we only flush()
        so child rows can reference parent's id.

        `acl_filter` is the Qdrant pre-filter built by the router via
        `acl_filter.build_user_filter(ctx, session)`. Stored on
        `self._acl_filter` for the request lifecycle so both the
        direct path (TwoStageSearcher) and the agent path
        (AgentRunner) scope retrieval to the caller's permitted
        documents. Without this, the searcher returns 0 hits and
        the synthesizer emits an empty answer — the root cause of
        the 2026-09-03 empty-answer bug. P0 / 2026-09-03.
        """
        if not request.query or not request.query.strip():
            raise EmptyQueryError("query must be a non-empty string")

        conversation = self._ensure_conversation(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id_str=request.conversation_id,
            default_title=default_conversation_title or request.query[:60],
        )
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=request.query,
            metadata_={"workspace_id": str(workspace_id)},
        )
        session.add(user_message)
        session.flush()

        history_prompt = self._load_history_prompt(conversation.id)

        route_decision = self._decide_route(request.query)
        # Stash for the request lifecycle — both paths read it.
        self._acl_filter = acl_filter

        if route_decision.is_direct():
            outcome: DirectPathOutcome | AgentPathOutcome = self._execute_direct(
                query=request.query,
                history_prompt=history_prompt,
            )
            response_route = "direct"
        else:
            outcome = self._execute_agent(
                session=session,
                query=request.query,
                history_prompt=history_prompt,
                max_iterations=request.max_iterations
                or self.default_max_iterations,
            )
            if outcome.long_context_override is not None:
                response_route = "long_context"
            else:
                response_route = "agent"

        raw_answer = self._raw_answer(outcome, response_route)
        refused = False
        answer = raw_answer
        if self.sensitive_filter is not None:
            answer, refused = self.sensitive_filter.filter_or_refuse(raw_answer)
        redactions: dict[str, int] = {}
        if self.masker is not None and answer:
            answer = self.masker.mask(answer)
            redactions = self.masker.last_summary

        assistant_metadata = self._build_assistant_metadata(
            response_route=response_route,
            outcome=outcome,
            refused=refused,
            redactions=redactions,
        )
        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            metadata_=assistant_metadata,
        )
        session.add(assistant_message)
        session.flush()

        citations = self._outcome_citations(outcome, response_route)
        if citations:
            record_citations(
                session,
                message_id=assistant_message.id,
                search_results=[
                    SearchResult(
                        chunk_id=c.chunk_id,
                        document_id="",
                        content="",
                        score=c.relevance_score,
                        payload={
                            "document_name": c.document_name,
                            "page_no": c.page_no,
                        },
                    )
                    for c in citations
                ],
            )
        session.flush()

        return ChatQueryResponse(
            conversation_id=str(conversation.id),
            message_id=str(assistant_message.id),
            answer=answer,
            citations=citations,
            steps=self._outcome_steps(outcome, response_route),
            route=response_route,
            iterations=self._outcome_iterations(outcome, response_route),
            fallback_triggered=self._outcome_fallback_triggered(
                outcome, response_route
            ),
            truncated_by_max_iter=bool(
                getattr(outcome, "result", None)
                and outcome.result.truncated_by_max_iter
            ),
            refused=refused,
            redactions=redactions,
            metadata=self._outcome_top_metadata(outcome, response_route),
        )

    # ------------------------------------------------------------------
    # conversation + history
    # ------------------------------------------------------------------

    def _ensure_conversation(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        conversation_id_str: str | None,
        default_title: str,
    ) -> Conversation:
        if conversation_id_str:
            try:
                cid = uuid.UUID(conversation_id_str)
            except (KeyError, ValueError) as exc:
                raise ChatServiceError(
                    f"invalid conversation_id: {conversation_id_str}"
                ) from exc
            row = session.get(Conversation, cid)
            if row is None:
                raise ChatServiceError("conversation not found")
            if row.user_id != user_id:
                raise ChatServiceError(
                    "conversation does not belong to the caller"
                )
            if row.workspace_id != workspace_id:
                raise ChatServiceError(
                    "conversation belongs to a different workspace"
                )
            return row
        conv = Conversation(
            user_id=user_id,
            workspace_id=workspace_id,
            title=default_title,
        )
        session.add(conv)
        session.flush()
        return conv

    def _load_history_prompt(self, conversation_id: uuid.UUID) -> str:
        if self.memory is None:
            return ""
        turns: list[HistoryTurn] = self.memory.load_history(str(conversation_id))
        return self.memory.format_history_for_prompt(
            turns, max_chars=self.history_max_chars
        )

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------

    def _decide_route(self, query: str) -> RouteDecision:
        if self.router is None:
            return RouteDecision(
                route="agent",
                confidence=0.0,
                reason="no router configured",
                source="fallback",
            )
        try:
            return self.router.route(query)
        except Exception as exc:
            logger.warning("router.route failed: %s — defaulting to agent", exc)
            return RouteDecision(
                route="agent",
                confidence=0.0,
                reason=f"router error: {exc}",
                source="fallback",
            )

    def _build_acl_filter(
        self,
        workspace_id: uuid.UUID,
        extra: dict[str, Any] | None,
    ) -> qmodels.Filter | None:
        """DEPRECATED — kept only for backward-compatible signature.

        The actual filter is now built by the router via
        `acl_filter.build_user_filter(ctx, session)` and threaded
        straight into `handle(acl_filter=...)`. This stub is
        removed by 2026-09-03 / P0 workspace_id_pipeline work — see
        docs/workspace_id_pipeline/. When removed, callers see
        AttributeError, which is the intended loud-fail signal.
        """
        # Build a minimal filter using only the workspace scope so
        # any leftover caller (tests / admin tooling) still gets
        # *some* scoping instead of an open retriever. Direct
        # callers in this repo are gone after T10–T11.
        return qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="workspace_id",
                    match=qmodels.MatchValue(value=str(workspace_id)),
                )
            ]
        )

    # ------------------------------------------------------------------
    # path: direct
    # ------------------------------------------------------------------

    def _execute_direct(
        self,
        *,
        query: str,
        history_prompt: str,
    ) -> DirectPathOutcome:
        ts: TwoStageResult = self.two_stage.two_stage_search(
            query, acl_filter=self._acl_filter
        )
        # `fallback_triggered` here means two different things depending
        # on the TwoStageSearcher path:
        #   * stage-1 produced parents but the top score was below
        #     `fallback_threshold` — long-document intent switch; the
        #     caller wants the long-context model, not direct synthesis.
        #   * stage-1 produced NO parents and the searcher fell back to
        #     a single-stage search (2026-09-04, when the indexer only
        #     writes children to Qdrant). In that case `ts.children`
        #     carries the single-stage hits and we synthesise from them
        #     directly — falling back to empty here would silently kill
        #     every short-document query.
        # So the "no answer" path is only when we have NO children at
        # all; otherwise we synthesise from whatever the searcher
        # returned and surface `fallback_triggered` on the outcome so
        # observability still sees the long-context intent signal.
        if not ts.children:
            return DirectPathOutcome(
                answer="",
                citations=[],
                route="direct",
                fallback_triggered=ts.fallback_triggered,
            )
        hits = ts.children[: self.direct_top_k]
        citations = [
            CitationItem(
                chunk_id=h.chunk_id,
                document_name=str(h.payload.get("document_name", "")),
                page_no=_coerce_page_no(h.payload.get("page")),
                relevance_score=float(h.score),
            )
            for h in hits
        ]
        prompt = self._build_direct_prompt(query, hits, history_prompt)
        # Build per-call knobs from Settings. Direct synth defaults to
        # thinking OFF — qwen3.7-plus is a reasoning model and the
        # 3077 reasoning_tokens on a real RAG prompt are pure waste
        # for a direct lookup. See Step 4 / 2026-09-05.
        from agentic_rag_project.config import get_settings

        settings = get_settings()
        synth_opts: dict = {}
        if settings.direct_synth_max_tokens:
            synth_opts["max_tokens"] = settings.direct_synth_max_tokens
        if not settings.direct_synth_enable_thinking:
            synth_opts["extra_body"] = {"enable_thinking": False}
        answer = self.direct_synthesizer.synthesize(
            system_prompt=(
                "You are an answer synthesizer for a corporate RAG system. "
                "Use ONLY the provided context. Cite each claim with the "
                "provided `[chunk_id]`."
            ),
            user_prompt=prompt,
            # 45s per-attempt budget. qwen3.7-plus is a reasoning model whose
            # thinking trace on a real RAG prompt (≈4.6KB context, top-5 chunks)
            # measured 34.7s end-to-end on 2026-09-05 (reasoning_tokens=3077,
            # completion_tokens=1994). The prior 20s budget cut every real
            # direct-path synthesis off mid-generation → litellm.Timeout → 500.
            # 45s covers the observed 35s with ~10s headroom; the single retry
            # in completion_with_metrics is reserved for genuine transient
            # stalls, not normal generation time.
            timeout=45.0,
            **synth_opts,
        )
        answer = answer[: self.direct_answer_chars]
        return DirectPathOutcome(
            answer=answer,
            citations=citations,
            route="direct",
            fallback_triggered=ts.fallback_triggered,
        )

    def _build_direct_prompt(
        self,
        query: str,
        hits: list[SearchResult],
        history_prompt: str,
    ) -> str:
        blocks: list[str] = []
        if history_prompt:
            blocks.append(f"[history]\n{history_prompt}")
        for h in hits:
            blocks.append(f"[{h.chunk_id}]\n{h.content}")
        blocks.append(f"[question]\n{query}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # path: agent
    # ------------------------------------------------------------------

    def _execute_agent(
        self,
        *,
        session: Session,
        query: str,
        history_prompt: str,
        max_iterations: int,
    ) -> AgentPathOutcome:
        # The runner's `.run()` builds its own AgentState internally,
        # so we just pass query + max_iterations as kwargs. The
        # per-call `acl_filter` overrides the runner's constructor
        # default so this request scopes retrieval to the caller.
        try:
            result = self.agent_runner.run(
                query,
                user_context={"max_iterations": max_iterations},
                history=None,
                acl_filter=self._acl_filter,
            )
        except AgentRunnerError as exc:
            raise ChatServiceError(f"agent runner failed: {exc}") from exc
        except Exception as exc:
            # Defense in depth: any unexpected runner failure surfaces
            # as a single ChatServiceError so the router can map it
            # to a 500\n.
            logger.exception("agent runner raised unexpected error")
            raise ChatServiceError(f"agent runner failed: {exc}") from exc

        citations = [
            CitationItem(
                chunk_id=r.chunk_id,
                document_name="",
                page_no=_coerce_page_no(r.payload.get("page")),
                relevance_score=float(r.score),
            )
            for r in result.citations
        ]
        citations = self._hydrate_document_names(session, citations)

        # The runner doesn't surface a separate `parents` list — we
        # derive it from `result.citations` filtered on the payload
        # flag the indexer sets (`is_parent=True`). When none qualify
        # we fall through to the agent's answer as-is.
        parents = [
            r
            for r in result.citations
            if bool(r.payload.get("is_parent", False))
        ]

        override: LongContextResult | None = None
        if result.fallback_triggered and parents:
            try:
                override = self.long_context.generate(
                    query=query,
                    parents=parents,
                    history=None,
                )
            except Exception as exc:
                # Broad catch on purpose: the long-context path is a
                # *best-effort* upgrade — the agent answer is already
                # good enough. Any failure (timeout, rate-limit, OOM,
                # custom LongContextFallbackError, ...) keeps the
                # caller from seeing a 500 when we have a usable
                # answer in hand.
                logger.warning(
                    "long-context fallback failed: %s — using agent answer",
                    exc,
                )
                override = None

        return AgentPathOutcome(
            result=result, citations=citations, long_context_override=override
        )

    def _hydrate_document_names(
        self, session: Session, citations: list[CitationItem]
    ) -> list[CitationItem]:
        if not citations:
            return citations
        chunk_uuids = [
            uuid.uuid5(uuid.NAMESPACE_DNS, f"agentic-rag-project/{c.chunk_id}")
            for c in citations
        ]
        stmt = (
            select(Chunk.id, Document.name)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id.in_(chunk_uuids))
        )
        name_by_uuid = {row[0]: row[1] for row in session.execute(stmt).all()}
        out: list[CitationItem] = []
        for c in citations:
            cid_uuid = uuid.uuid5(
                uuid.NAMESPACE_DNS, f"agentic-rag-project/{c.chunk_id}"
            )
            name = name_by_uuid.get(cid_uuid)
            out.append(
                CitationItem(
                    chunk_id=c.chunk_id,
                    document_name=name or c.document_name,
                    page_no=c.page_no,
                    relevance_score=c.relevance_score,
                )
            )
        return out

    # ------------------------------------------------------------------
    # outcome → response
    # ------------------------------------------------------------------

    def _raw_answer(
        self, outcome: DirectPathOutcome | AgentPathOutcome, route: str
    ) -> str:
        if isinstance(outcome, DirectPathOutcome):
            return outcome.answer
        if outcome.long_context_override is not None:
            return outcome.long_context_override.answer
        return outcome.result.answer

    def _outcome_citations(
        self,
        outcome: DirectPathOutcome | AgentPathOutcome,
        route: str,
    ) -> list[CitationItem]:
        if isinstance(outcome, DirectPathOutcome):
            return outcome.citations
        return outcome.citations

    def _outcome_steps(
        self,
        outcome: DirectPathOutcome | AgentPathOutcome,
        route: str,
    ) -> list[StepItem]:
        if isinstance(outcome, DirectPathOutcome):
            return [
                StepItem(
                    step_id="direct",
                    iteration=0,
                    node="direct",
                    action="two-stage retrieval + synthesis",
                    detail=f"citations={len(outcome.citations)}",
                )
            ]
        items = [_agent_step_to_item(s) for s in outcome.result.steps]
        if outcome.long_context_override is not None:
            items.append(
                StepItem(
                    step_id="long_context",
                    iteration=outcome.result.iterations + 1,
                    node="long_context",
                    action="long-context fallback",
                    detail=(
                        f"model={outcome.long_context_override.model} "
                        f"parents={len(outcome.long_context_override.parents_used)}"
                    ),
                )
            )
        return items

    def _outcome_iterations(
        self,
        outcome: DirectPathOutcome | AgentPathOutcome,
        route: str,
    ) -> int:
        if isinstance(outcome, DirectPathOutcome):
            return 0
        return int(outcome.result.iterations)

    def _outcome_fallback_triggered(
        self,
        outcome: DirectPathOutcome | AgentPathOutcome,
        route: str,
    ) -> bool:
        if isinstance(outcome, DirectPathOutcome):
            return bool(outcome.fallback_triggered)
        if outcome.long_context_override is not None:
            return True
        return bool(outcome.result.fallback_triggered)

    def _outcome_top_metadata(
        self,
        outcome: DirectPathOutcome | AgentPathOutcome,
        route: str,
    ) -> dict[str, Any]:
        if isinstance(outcome, DirectPathOutcome):
            return {"source": "direct"}
        if outcome.long_context_override is not None:
            return outcome.long_context_override.to_metadata()
        return {"source": "agent", "iterations": int(outcome.result.iterations)}

    def _build_assistant_metadata(
        self,
        *,
        response_route: str,
        outcome: DirectPathOutcome | AgentPathOutcome,
        refused: bool,
        redactions: dict[str, int],
    ) -> dict[str, Any]:
        md: dict[str, Any] = self._outcome_top_metadata(outcome, response_route)
        if refused:
            md["refused"] = True
        if redactions:
            md["redactions"] = dict(redactions)
        return md


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def _coerce_page_no(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, (str, float)):
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None
    return None


def _agent_step_to_item(step: AgentStep) -> StepItem:
    # Coerce non-string payloads to a JSON string so the API contract holds
    # regardless of what individual nodes emit. `AgentStep.detail` is typed
    # `dict[str, Any]` (state.py:52) but `StepItem.detail` is typed `str`
    # (schema.py:34) — without this, `plan_node`'s dict payload
    # (`{"sub_queries": [...], "rationale": ..., "fallback": bool}`) triggers
    # a Pydantic ValidationError that the chat_router catch-all turns into
    # HTTP 500 "internal_error". Surfaced 2026-08-26 during M3 Live E2E
    # (see docs/m3_agentic_rag_intent/).
    detail = step.detail
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    return StepItem(
        step_id=step.step_id,
        iteration=step.iteration,
        node=step.node,
        action=step.action,
        detail=detail,
        duration_ms=int(step.duration_ms),
    )


__all__ = [
    "AgentPathOutcome",
    "ChatService",
    "ChatServiceError",
    "DirectPathOutcome",
    "EmptyQueryError",
    "LLMSynthesizer",
]
