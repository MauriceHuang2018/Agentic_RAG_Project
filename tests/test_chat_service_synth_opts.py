"""Direct-path synth opts forwarding — closed 2026-09-05 Step 8.

Verifies that chat_service._execute_direct threads the per-call
knobs from Settings.direct_synth_* into the synthesizer:

  * direct_synth_max_tokens        -> synthesize(..., max_tokens=N)
  * direct_synth_enable_thinking   -> synthesize(..., extra_body={"enable_thinking": False})

The timeout=45.0 safety net and answer[:direct_answer_chars] cap
must remain unchanged regardless of the opts.

We test `_execute_direct` directly (not `handle`) because that is
where the synth call lives, and it avoids dragging in the full
session/router/agent dependency graph.
"""

from __future__ import annotations

import pytest

from agentic_rag_project.config import get_settings
from agentic_rag_project.retrieval_direct.search import SearchResult


class _CaptureSynth:
    """Minimal LLMSynthesizer stand-in that records the kwargs it received."""

    def __init__(self, answer: str = "captured-answer") -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def synthesize(self, *, system_prompt, user_prompt, timeout, **kwargs):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        return self.answer


class _StubTwoStage:
    """TwoStageSearcher stand-in that returns a fixed result."""

    def __init__(self, children: list[SearchResult], parents: list[SearchResult]) -> None:
        self._children = children
        self._parents = parents

    def two_stage_search(self, query, *, acl_filter=None):
        from agentic_rag_project.retrieval_direct.two_stage import TwoStageResult

        return TwoStageResult(
            parents=self._parents,
            children=self._children,
            fallback_triggered=False,
        )


def _child(cid: str) -> SearchResult:
    return SearchResult(
        chunk_id=cid,
        document_id="d",
        content="child body",
        score=0.8,
        payload={"document_name": "doc", "page": 1},
    )


def _parent(pid: str) -> SearchResult:
    return SearchResult(
        chunk_id=pid,
        document_id="d",
        content="parent body",
        score=0.9,
        payload={"document_name": "doc"},
    )


def _build_service(capture: _CaptureSynth, children, parents):
    """Build a ChatService with stubs — only direct_synthesizer is exercised."""
    from agentic_rag_project.chat.chat_service import ChatService
    from agentic_rag_project.query_guardrail.checker import SensitiveWordFilter

    class _StubRunner:
        def run(self, *args, **kwargs):
            raise RuntimeError("agent path not used in this test")

    class _StubRouter:
        def route(self, query):
            from agentic_rag_project.router.classifier import RouteDecision

            return RouteDecision(
                route="direct", confidence=0.9, reason="forced", source="test"
            )

    class _StubMemory:
        def __init__(self) -> None:
            self.turns: list = []

    class _StubLongContext:
        def generate(self, *, query, parents, history):
            return None

    class _StubSearcher:
        def hybrid_search(self, *a, **kw):
            return []

    svc = ChatService(
        searcher=_StubSearcher(),  # type: ignore[arg-type]
        two_stage=_StubTwoStage(children, parents),  # type: ignore[arg-type]
        router=_StubRouter(),  # type: ignore[arg-type]
        agent_runner=_StubRunner(),  # type: ignore[arg-type]
        long_context=_StubLongContext(),  # type: ignore[arg-type]
        memory=_StubMemory(),  # type: ignore[arg-type]
        sensitive_filter=SensitiveWordFilter(words=(), refusal_text="REFUSED"),
        masker=None,
        direct_synthesizer=capture,  # type: ignore[arg-type]
    )
    # _acl_filter is normally injected via lifespan → handle(). When we
    # call _execute_direct directly we set it here so two_stage_search
    # gets the right filter shape.
    svc._acl_filter = None
    return svc


def test_execute_direct_forwards_enable_thinking_false_and_max_tokens() -> None:
    """Defaults from .env (Step 4) must reach the synthesizer verbatim."""
    get_settings.cache_clear()
    capture = _CaptureSynth()
    svc = _build_service(capture, [_child("c1")], [_parent("p1")])

    outcome = svc._execute_direct(query="Platform Analytics是什么？", history_prompt="")

    assert len(capture.calls) == 1, "synthesizer should be invoked exactly once"
    call = capture.calls[0]
    # Per Step 4 defaults — see tests/test_config.py.
    assert call["kwargs"].get("max_tokens") == 1024
    assert call["kwargs"].get("extra_body") == {"enable_thinking": False}
    # timeout=45.0 safety net preserved.
    assert call["timeout"] == pytest.approx(45.0)
    # Defense-in-depth slice preserved.
    assert len(outcome.answer) <= 4000


def test_execute_direct_keeps_answer_chars_4000_cap() -> None:
    """Even with max_tokens=1024, the answer hard-cap must hold."""
    get_settings.cache_clear()
    long_answer = "x" * 5000
    capture = _CaptureSynth(answer=long_answer)
    svc = _build_service(capture, [_child("c1")], [_parent("p1")])

    outcome = svc._execute_direct(query="foo bar baz", history_prompt="")

    assert len(outcome.answer) == 4000, (
        f"defensive slice lost: got {len(outcome.answer)} chars, expected 4000"
    )


def test_execute_direct_no_knobs_when_settings_zeroed(monkeypatch) -> None:
    """Settings cleared → no extra_body / no max_tokens forwarded."""
    get_settings.cache_clear()
    monkeypatch.setenv("DIRECT_SYNTH_MAX_TOKENS", "0")
    monkeypatch.setenv("DIRECT_SYNTH_ENABLE_THINKING", "true")
    get_settings.cache_clear()

    capture = _CaptureSynth()
    svc = _build_service(capture, [_child("c1")], [_parent("p1")])
    svc._execute_direct(query="x", history_prompt="")

    call = capture.calls[0]
    assert "max_tokens" not in call["kwargs"]
    assert "extra_body" not in call["kwargs"]
    # timeout preserved regardless.
    assert call["timeout"] == pytest.approx(45.0)