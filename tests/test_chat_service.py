"""Tests for `ChatService` (T3 wire-up, service layer).

Tests inject fakes for every collaborator so they exercise only the
service's own logic (routing decision, post-processing order,
outcome-to-response mapping). The fake `Session` records writes
without hitting a database; `record_citations` is monkey-patched to
a no-op so we don't need the `chunks` / `documents` tables populated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentic_rag_project.agent_core.state import (
    AgentResult,
    AgentStep,
)
from agentic_rag_project.chat import (
    ChatQueryRequest,
    ChatService,
    ChatServiceError,
    EmptyQueryError,
)
from agentic_rag_project.post_processor.filter import SensitiveWordFilter
from agentic_rag_project.post_processor.mask import Masker
from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.two_stage import (
    TwoStageSearcher,
)
from agentic_rag_project.router.classifier import RouteDecision


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSearcher:
    """Implements the `SearcherLike` protocol with a fixed hit list."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def hybrid_search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float | None = None,
        qdrant_filter: Any = None,
    ) -> list[SearchResult]:
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "score_threshold": score_threshold,
                "qdrant_filter": qdrant_filter,
            }
        )
        return []


class FakeRouter:
    """Programmable ConfidenceRouter replacement."""

    def __init__(self, decision: RouteDecision) -> None:
        self._decision = decision
        self.calls = 0

    def route(self, query: str) -> RouteDecision:
        self.calls += 1
        return self._decision


class FakeAgentRunner:
    """Returns a fixed AgentResult without running the graph."""

    def __init__(self, result: AgentResult) -> None:
        self._result = result
        self.calls = 0

    def run(
        self,
        query: str,
        *,
        user_context: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        history=None,
    ) -> AgentResult:
        self.calls += 1
        return self._result


@dataclass
class StubLongContext:
    """Records calls to `generate` and returns a scripted result."""

    result: Any | None = None
    raise_exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(self, *, query, parents, history):
        self.calls.append(
            {"query": query, "parents": parents, "history": history}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


@dataclass
class StubDirectSynth:
    """Returns a fixed answer for the direct path."""

    answer: str = "direct-answer"
    calls: int = 0

    def synthesize(self, *, system_prompt, user_prompt, timeout):
        self.calls += 1
        return self.answer


class FakeMemory:
    """In-memory replacement for ConversationMemory."""

    def __init__(self, turns: list | None = None) -> None:
        self._turns = turns or []
        self.load_calls = 0

    def load_history(self, conversation_id):
        self.load_calls += 1
        return list(self._turns)

    def format_history_for_prompt(self, turns, *, max_chars: int = 4000):
        """Trivial formatter that mirrors ConversationMemory's behavior."""
        if not turns:
            return ""
        out: list[str] = []
        used = 0
        for t in reversed(turns):
            line = f"[{t.role}] {t.content}"
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
        return "\n".join(reversed(out))


# ---------------------------------------------------------------------------
# Fake SQLAlchemy session
# ---------------------------------------------------------------------------


class FakeSession:
    """Lightweight Session stub supporting the operations ChatService uses."""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed = 0
        self._store: dict = {}

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1
        for obj in self.added:
            tbl = type(obj)
            bucket = self._store.setdefault(tbl, {})
            bucket[obj.id if hasattr(obj, "id") else id(obj)] = obj

    def get(self, cls, pk):
        bucket = self._store.get(cls, {})
        return bucket.get(pk)

    def execute(self, stmt):
        class _Result:
            def all(inner_self):
                return []

        return _Result()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_session(monkeypatch) -> FakeSession:
    """Fake session + stub `record_citations` to avoid touching DB."""
    monkeypatch.setattr(
        "agentic_rag_project.chat.chat_service.record_citations",
        lambda session, *, message_id, search_results: [],
    )
    return FakeSession()


def _make_search_result(
    chunk_id: str, score: float = 0.9, page: int | None = None
) -> SearchResult:
    payload: dict[str, Any] = {"document_name": "report.pdf"}
    if page is not None:
        payload["page"] = page
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=f"content for {chunk_id}",
        score=score,
        payload=payload,
    )


def _make_agent_result(
    *,
    answer: str = "agent-answer",
    citations: list[SearchResult] | None = None,
    parents: list[SearchResult] | None = None,
    fallback_triggered: bool = False,
    truncated: bool = False,
    iterations: int = 2,
) -> AgentResult:
    """Build an AgentResult with optional parent citations for long-context."""
    cit = list(citations or [])
    # If parents is provided separately, merge them with child citations.
    if parents:
        cit = cit + parents
    return AgentResult(
        answer=answer,
        citations=cit,
        steps=[
            AgentStep(iteration=0, node="plan", action="ok", detail="", duration_ms=10),
            AgentStep(iteration=1, node="retrieve", action="ok", detail="", duration_ms=20),
        ],
        iterations=iterations,
        fallback_triggered=fallback_triggered,
        truncated_by_max_iter=truncated,
    )


def _make_parent_result(chunk_id: str) -> SearchResult:
    """A SearchResult with `is_parent=True` payload, qualifying for long-context."""
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=f"parent content {chunk_id}",
        score=0.9,
        payload={"document_name": "report.pdf", "is_parent": True},
    )


def _make_long_context_result(answer: str = "[long-context] long answer"):
    """Build a stand-in object with the LongContextResult shape."""

    class _LCR:
        def __init__(self):
            self.answer = answer
            self.parents_used = ["p-1"]
            self.model = "claude-sonnet-4-5"
            self.truncated = False

        def to_metadata(self):
            return {
                "source": "long-context",
                "model": "claude-sonnet-4-5",
                "parents_used": ["p-1"],
                "truncated": False,
            }

    return _LCR()


def _make_service(
    *,
    fake_session: FakeSession,
    router_decision: RouteDecision,
    agent_result: AgentResult | None = None,
    direct_answer: str = "direct-answer",
    two_stage_children: list[SearchResult] | None = None,
    long_context: StubLongContext | None = None,
    sensitive_words: tuple = (),
    history: list | None = None,
) -> ChatService:
    searcher = FakeSearcher()

    # Wrap hybrid_search so stage 1 returns a parent and stage 2 returns
    # the configured children (TwoStageSearcher calls hybrid_search twice
    # per query: once for parents, once for children).
    call_count = {"n": 0}

    def _hybrid_search(query, *, top_k, score_threshold=None, qdrant_filter=None):
        searcher.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "score_threshold": score_threshold,
                "qdrant_filter": qdrant_filter,
            }
        )
        call_count["n"] += 1
        if call_count["n"] % 2 == 1:
            # Stage 1 — return one parent so two_stage_search proceeds.
            return [_make_search_result(f"p-{call_count['n']}", score=0.9)]
        # Stage 2 — return the configured children.
        return two_stage_children or []

    searcher.hybrid_search = _hybrid_search  # type: ignore[assignment]

    two_stage = TwoStageSearcher(
        searcher,  # type: ignore[arg-type]
        parent_top_k=3,
        child_top_k=10,
        fallback_threshold=0.5,
    )
    router = FakeRouter(router_decision)
    runner = FakeAgentRunner(agent_result or _make_agent_result())
    lc = long_context or StubLongContext(result=None)
    sensitive = SensitiveWordFilter(words=sensitive_words, refusal_text="REFUSED")
    masker = Masker()
    memory = FakeMemory(history)
    direct = StubDirectSynth(answer=direct_answer)

    return ChatService(
        searcher=searcher,  # type: ignore[arg-type]
        two_stage=two_stage,
        router=router,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        long_context=lc,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        sensitive_filter=sensitive,
        masker=masker,
        direct_synthesizer=direct,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_handle_rejects_empty_query(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
    )
    req = ChatQueryRequest(query="   ")
    with pytest.raises(EmptyQueryError):
        svc.handle(
            session=fake_session,
            request=req,
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# Conversation lifecycle
# ---------------------------------------------------------------------------


def test_handle_creates_conversation_when_id_missing(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
    )
    req = ChatQueryRequest(query="hello")
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    resp = svc.handle(
        session=fake_session,
        request=req,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    types_added = [type(x).__name__ for x in fake_session.added]
    assert "Conversation" in types_added
    assert types_added.count("Message") == 2
    assert resp.conversation_id
    assert resp.message_id


def test_handle_uses_provided_conversation_id(fake_session: FakeSession) -> None:
    from agentic_rag_project.db.models.conversations import Conversation

    conversation_id = uuid.uuid4()
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    conv = Conversation(
        id=conversation_id,
        user_id=user_id,
        workspace_id=workspace_id,
        title="prior",
    )
    # Store under the real class so `session.get(Conversation, id)` finds it.
    fake_session._store[Conversation] = {conversation_id: conv}

    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
    )
    req = ChatQueryRequest(query="hi", conversation_id=str(conversation_id))
    resp = svc.handle(
        session=fake_session,
        request=req,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    assert resp.conversation_id == str(conversation_id)


def test_handle_rejects_mismatched_user(fake_session: FakeSession) -> None:
    from agentic_rag_project.db.models.conversations import Conversation

    conversation_id = uuid.uuid4()

    conv = Conversation(
        id=conversation_id,
        user_id=uuid.uuid4(),  # different user
        workspace_id=uuid.uuid4(),
        title="prior",
    )
    fake_session._store[Conversation] = {conversation_id: conv}

    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
    )
    req = ChatQueryRequest(query="hi", conversation_id=str(conversation_id))
    with pytest.raises(ChatServiceError):
        svc.handle(
            session=fake_session,
            request=req,
            user_id=uuid.uuid4(),
            workspace_id=conv.workspace_id,
        )


# ---------------------------------------------------------------------------
# Direct path
# ---------------------------------------------------------------------------


def test_direct_path_returns_assistant_answer(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="direct", confidence=0.95, reason="test", source="llm"),
        direct_answer="the quick brown fox",
        two_stage_children=[
            _make_search_result("c-1", score=0.9, page=2),
            _make_search_result("c-2", score=0.8),
        ],
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="what?"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.route == "direct"
    assert resp.answer == "the quick brown fox"
    assert len(resp.citations) == 2
    assert resp.citations[0].page_no == 2
    assert resp.steps and resp.steps[0].node == "direct"


# ---------------------------------------------------------------------------
# Agent path
# ---------------------------------------------------------------------------


def test_agent_path_runs_runner_and_returns_citations(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(
            answer="multi-hop answer",
            citations=[_make_search_result("c-a"), _make_search_result("c-b")],
        ),
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="why?"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.route == "agent"
    assert resp.answer == "multi-hop answer"
    assert resp.iterations == 2
    assert len(resp.citations) == 2
    assert resp.metadata.get("source") == "agent"


def test_agent_path_invokes_long_context_on_fallback(fake_session: FakeSession) -> None:
    parent = _make_parent_result("p-1")
    lc = StubLongContext(result=_make_long_context_result())
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(
            answer="agent answer",
            citations=[_make_search_result("c-a")],
            parents=[parent],
            fallback_triggered=True,
        ),
        long_context=lc,
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.route == "long_context"
    assert resp.answer.startswith("[long-context]")
    assert resp.fallback_triggered is True
    assert lc.calls, "long_context should have been invoked"


def test_agent_path_continues_when_long_context_raises(fake_session: FakeSession) -> None:
    lc = StubLongContext(
        result=None,
        raise_exc=Exception("llm 500"),
    )
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(
            answer="agent fallback answer",
            citations=[_make_search_result("c-a")],
            parents=[_make_parent_result("p-1")],
            fallback_triggered=True,
        ),
        long_context=lc,
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.route == "agent"
    assert resp.answer == "agent fallback answer"


def test_agent_path_records_truncation_flag(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(answer="answer", truncated=True, iterations=5),
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.truncated_by_max_iter is True
    assert resp.iterations == 5


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def test_sensitive_word_filter_swaps_answer(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(
            answer="warns about 毒品 here",
        ),
        sensitive_words=("毒品",),
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.refused is True
    assert "毒品" not in resp.answer
    assert resp.answer == "REFUSED"


def test_masker_redacts_phone_number(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(answer="call 13800138000 today"),
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert "13800138000" not in resp.answer
    assert resp.redactions.get("phone") == 1


def test_post_processing_order_filter_then_mask(fake_session: FakeSession) -> None:
    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(answer="warns 毒品 and 13800138000"),
        sensitive_words=("毒品",),
    )
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.refused is True
    assert resp.redactions == {}, "masker should not run on refusal text"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_chat_service_error_propagates_on_runner_failure(fake_session: FakeSession) -> None:
    class FailingRunner(FakeAgentRunner):
        def run(
            self,
            query: str,
            *,
            user_context=None,
            conversation_id=None,
            history=None,
        ):
            raise Exception("boom")

    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
    )
    svc.agent_runner = FailingRunner(_make_agent_result())  # type: ignore[assignment]
    with pytest.raises(ChatServiceError):
        svc.handle(
            session=fake_session,
            request=ChatQueryRequest(query="q"),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
        )


def test_router_failure_falls_back_to_agent(fake_session: FakeSession) -> None:
    class FailingRouter:
        def route(self, query):
            raise RuntimeError("classifier unavailable")

    svc = _make_service(
        fake_session=fake_session,
        router_decision=RouteDecision(route="agent", confidence=0.9, reason="test", source="llm"),
        agent_result=_make_agent_result(answer="ok"),
    )
    svc.router = FailingRouter()  # type: ignore[assignment]
    resp = svc.handle(
        session=fake_session,
        request=ChatQueryRequest(query="q"),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
    )
    assert resp.route == "agent"
    assert resp.answer == "ok"
