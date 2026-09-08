"""Tests for the agent core (T3.2).

Covers:
  * AgentStep serialization
  * AgentResult helpers
  * State init and bookkeeping
  * Calculator: safe arithmetic + reject bad chars
  * Web search stub returns deterministic placeholder
  * Retrieval tool: dedup-preserving-order
  * Plan node: produce sub_queries on first call, advance on subsequent
  * Reflect node: parses `continue` / `rewrite` / `answer`
  * Synthesize node: extracts `[chunk_id]` citations and caps length
  * AgentRunner: full happy path with fake LLM + fake retriever
  * AgentRunner: max_iter cap truncates and still synthesizes
  * AgentRunner: plan fallback when LLM returns garbage
  * AgentRunner: rewrite path re-issues sub-query
  * AgentRunner: empty query raises
  * AgentRunner: invalid max_iterations rejected
  * LangGraph build_agent_graph compiles without raising
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from agentic_rag_project.agent_core import (
    AgentResult,
    AgentRunner,
    AgentRunnerError,
    AgentStep,
    CachedRetrievalTool,
    DEFAULT_MAX_ITERATIONS,
    ReflectDecision,
    build_agent_graph,
    make_initial_state,
    run_calculator,
    run_retrieval,
    run_web_search,
    search_results_to_state_payload,
)
from agentic_rag_project.agent_core.nodes import (
    _extract_json,
    plan_node,
    reflect_node,
    retrieve_node,
    rewrite_node,
    should_continue,
    synthesize_node,
)
from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.two_stage import TwoStageResult


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _make_hit(chunk_id: str, score: float = 0.9, content: str = "x") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=content or f"content-{chunk_id}",
        score=score,
        payload={"chunk_id": chunk_id},
    )


def _make_two_stage(hits: list[SearchResult]) -> TwoStageResult:
    return TwoStageResult(
        parents=hits,
        children=[],
        fallback_triggered=False,
        top_score=hits[0].score if hits else 0.0,
    )


class FakeSearcher:
    def __init__(self, queue: list[list[SearchResult]] | None = None) -> None:
        self._queue = list(queue or [])
        self.calls: list[str] = []

    def two_stage_search(
        self, query: str, *, acl_filter: Any | None = None
    ) -> TwoStageResult:
        self.calls.append(query)
        if self._queue:
            return _make_two_stage(self._queue.pop(0))
        return _make_two_stage([])


class ScriptedLLM:
    """Returns pre-scripted responses in order; falls back to a default."""

    def __init__(
        self,
        responses: list[str],
        *,
        default: str | None = None,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, system: str, user: str, timeout: float) -> str:
        self.calls.append((system, user, timeout))
        if self._responses:
            return self._responses.pop(0)
        return self._default or ""


# ---------------------------------------------------------------------------
# AgentStep / AgentResult
# ---------------------------------------------------------------------------


def test_agent_step_to_dict_shape() -> None:
    step = AgentStep(iteration=1, node="plan", action="x", duration_ms=10)
    d = step.to_dict()
    for k in ("step_id", "iteration", "node", "action", "detail", "started_at", "duration_ms"):
        assert k in d


def test_agent_result_helpers() -> None:
    r = AgentResult(
        answer="hello [c1]",
        citations=[_make_hit("c1")],
        steps=[AgentStep(node="plan", action="x")],
    )
    sd = r.step_dicts()
    cd = r.citation_dicts()
    assert len(sd) == 1
    assert sd[0]["node"] == "plan"
    assert len(cd) == 1
    assert cd[0]["chunk_id"] == "c1"


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------


def test_make_initial_state_defaults() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    assert s["query"] == "q"
    assert s["iteration"] == 0
    assert s["max_iterations"] == DEFAULT_MAX_ITERATIONS
    assert s["retrieved_chunks"] == []
    assert s["history"] == []


def test_search_results_to_state_payload_dedups() -> None:
    hits = [_make_hit("c1"), _make_hit("c1"), _make_hit("c2")]
    dicts, ids = search_results_to_state_payload(hits)
    assert len(dicts) == 3  # dicts preserve all rows
    assert ids == ["c1", "c2"]  # ids dedup with order


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------


def test_calculator_basic_arithmetic() -> None:
    assert run_calculator("1 + 2") == "3"
    assert run_calculator("10 / 4") == "2.5"
    assert run_calculator("(1 + 2) * 3") == "9"
    assert run_calculator("100 % 7") == "2"


def test_calculator_trims_floats() -> None:
    out = run_calculator("0.1 + 0.2")
    # No noisy trailing zeros.
    assert "." not in out.split(".")[0] or out.endswith(("3", "0"))


def test_calculator_rejects_unsupported_characters() -> None:
    with pytest.raises(ValueError):
        run_calculator("__import__('os')")
    with pytest.raises(ValueError):
        run_calculator("print('x')")


def test_calculator_empty_returns_empty() -> None:
    assert run_calculator("") == ""
    assert run_calculator("   ") == ""


def test_calculator_rejects_bad_expression() -> None:
    with pytest.raises(ValueError):
        run_calculator("1/0")


# ---------------------------------------------------------------------------
# web search stub
# ---------------------------------------------------------------------------


def test_web_search_stub_returns_placeholder() -> None:
    out = run_web_search("company sales report")
    assert len(out) == 1
    assert "stub" in out[0]["snippet"].lower()


def test_web_search_respects_max_results() -> None:
    assert len(run_web_search("x", max_results=0)) == 0


# ---------------------------------------------------------------------------
# CachedRetrievalTool
# ---------------------------------------------------------------------------


def test_cached_retrieval_tool_dedups_results() -> None:
    s = FakeSearcher([[_make_hit("c1"), _make_hit("c1"), _make_hit("c2")]])
    tool = CachedRetrievalTool(s)  # type: ignore[arg-type]
    hits = tool.run("q")
    # Dedup: c1 appears once.
    assert [h.chunk_id for h in hits] == ["c1", "c2"]
    assert tool.fallback_triggered is False


def test_cached_retrieval_tool_propagates_fallback_flag() -> None:
    class FallbackSearcher:
        def two_stage_search(self, query: str, *, acl_filter=None):
            return TwoStageResult(
                parents=[_make_hit("c1", score=0.1)],
                children=[],
                fallback_triggered=True,
                top_score=0.1,
            )

    tool = CachedRetrievalTool(FallbackSearcher())  # type: ignore[arg-type]
    tool.run("q")
    assert tool.fallback_triggered is True


# ---------------------------------------------------------------------------
# _extract_json helper
# ---------------------------------------------------------------------------


def test_extract_json_handles_code_fence() -> None:
    assert _extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_handles_preamble() -> None:
    assert _extract_json("Sure! Here you go: {\"a\": 2}") == {"a": 2}


def test_extract_json_returns_none_for_garbage() -> None:
    assert _extract_json("not json at all") is None


def test_extract_json_returns_none_for_unbalanced() -> None:
    assert _extract_json("{\"a\": 1") is None


# ---------------------------------------------------------------------------
# Node-level behavior (LLM faked)
# ---------------------------------------------------------------------------


def test_plan_node_produces_sub_queries_on_first_call() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    llm = ScriptedLLM(
        ['{"sub_queries": ["sub-A", "sub-B"], "rationale": "two parts"}']
    )
    update = plan_node(s, llm_call=llm, timeout=1.0)
    assert update["sub_queries"] == ["sub-A", "sub-B"]
    # Steps were appended.
    assert any("plan" == step.get("node") for step in s["steps"])


def test_plan_node_falls_back_to_original_query_on_garbage() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    llm = ScriptedLLM(["not json"])
    update = plan_node(s, llm_call=llm, timeout=1.0)
    assert update["sub_queries"] == ["q"]


def test_rewrite_node_replaces_current_sub_query() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["sub_queries"] = ["bad"]
    s["current_sub_query_index"] = 0
    llm = ScriptedLLM(['{"sub_query": "good", "rationale": "better words"}'])
    update = rewrite_node(s, llm_call=llm, timeout=1.0)
    assert update["sub_queries"] == ["good"]


def test_retrieve_node_appends_hits_and_advances_index() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["sub_queries"] = ["sub"]
    s["current_sub_query_index"] = 0
    searcher = FakeSearcher([[_make_hit("c1"), _make_hit("c2")]])
    tool = CachedRetrievalTool(searcher)  # type: ignore[arg-type]
    update = retrieve_node(s, retrieval_tool=tool, acl_filter=None)
    assert update["retrieved_chunk_ids"] == ["c1", "c2"]
    assert update["current_sub_query_index"] == 1


def test_reflect_node_parses_decision() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["sub_queries"] = ["sub"]
    s["retrieved_chunks"] = [{"chunk_id": "c1"}]
    llm = ScriptedLLM(['{"decision": "answer", "reason": "enough"}'])
    update = reflect_node(s, llm_call=llm, timeout=1.0)
    assert update["reflect_decision"] == ReflectDecision.ANSWER.value


def test_reflect_node_defaults_to_answer_on_garbage() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    llm = ScriptedLLM(["nonsense"])
    update = reflect_node(s, llm_call=llm, timeout=1.0)
    assert update["reflect_decision"] == ReflectDecision.ANSWER.value


def test_synthesize_node_extracts_citations() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["retrieved_chunks"] = [
        asdict(_make_hit("c1", content="hello world")),
    ]
    llm = ScriptedLLM(["The answer cites [c1] for the main claim."])
    update = synthesize_node(s, llm_call=llm, timeout=1.0)
    assert "[c1]" in update["final_answer"]
    assert any(c["chunk_id"] == "c1" for c in update["citations"])


def test_synthesize_node_caps_answer_length() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    llm = ScriptedLLM(["x" * 5000])
    update = synthesize_node(s, llm_call=llm, timeout=1.0, max_answer_chars=200)
    assert len(update["final_answer"]) == 200


def test_should_continue_returns_false_at_max_iter() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["max_iterations"] = 2
    s["iteration"] = 2
    s["sub_queries"] = ["a", "b"]
    s["current_sub_query_index"] = 1
    s["reflect_decision"] = ReflectDecision.CONTINUE.value
    assert should_continue(s) is False


def test_should_continue_returns_false_when_reflect_says_answer() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["reflect_decision"] = ReflectDecision.ANSWER.value
    s["sub_queries"] = ["a"]
    s["current_sub_query_index"] = 0
    assert should_continue(s) is False


def test_should_continue_true_otherwise() -> None:
    s = make_initial_state(query="q", user_context={"user_id": "u1"})
    s["reflect_decision"] = ReflectDecision.CONTINUE.value
    s["sub_queries"] = ["a", "b"]
    s["current_sub_query_index"] = 0
    assert should_continue(s) is True


# ---------------------------------------------------------------------------
# AgentRunner end-to-end (fake LLM + fake retriever)
# ---------------------------------------------------------------------------


def test_runner_single_sub_query_happy_path() -> None:
    # Plan returns one sub-query; reflect says answer after first retrieve.
    llm = ScriptedLLM(
        [
            '{"sub_queries": ["only-one"], "rationale": "single lookup"}',
            '{"decision": "answer", "reason": "got it"}',
            "Final answer citing [c1].",
        ]
    )
    searcher = FakeSearcher([[_make_hit("c1", score=0.9)]])
    runner = AgentRunner(searcher=searcher, llm_call=llm, max_iterations=3)  # type: ignore[arg-type]
    result = runner.run(
        "what is X?",
        user_context={"user_id": "u1", "workspace_id": "w1"},
    )
    assert isinstance(result, AgentResult)
    assert "Final answer" in result.answer
    assert result.iterations == 1
    assert result.truncated_by_max_iter is False
    assert any(c.chunk_id == "c1" for c in result.citations)


def test_runner_max_iter_truncates_and_synthesizes() -> None:
    # Reflect always says `continue` — runner must hit the cap.
    llm_responses = [
        '{"sub_queries": ["a", "b"], "rationale": "split"}',  # plan
        '{"decision": "continue", "reason": "more"}',  # reflect iter 1
        '{"decision": "continue", "reason": "more"}',  # reflect iter 2
        "Final answer.",  # synthesize
    ]
    llm = ScriptedLLM(llm_responses)
    # Provide one hit per call so iteration can keep going.
    searcher = FakeSearcher(
        [
            [_make_hit("c1")],
            [_make_hit("c2")],
            [_make_hit("c3")],
            [_make_hit("c4")],
        ]
    )
    runner = AgentRunner(searcher=searcher, llm_call=llm, max_iterations=2)  # type: ignore[arg-type]
    result = runner.run(
        "q",
        user_context={"user_id": "u1"},
    )
    assert result.truncated_by_max_iter is True
    assert result.iterations == 2
    assert result.answer == "Final answer."


def test_runner_plan_failure_falls_back_to_single_subquery() -> None:
    llm = ScriptedLLM(
        [
            "not parseable json",  # plan -> fallback
            '{"decision": "answer", "reason": "ok"}',  # reflect
            "Answer.",  # synthesize
        ]
    )
    searcher = FakeSearcher([[_make_hit("c1")]])
    runner = AgentRunner(searcher=searcher, llm_call=llm, max_iterations=2)  # type: ignore[arg-type]
    result = runner.run(
        "what is X?",
        user_context={"user_id": "u1"},
    )
    assert result.answer == "Answer."
    # The fallback plan step should be recorded.
    assert any(
        "fallback" in step.action.lower() for step in result.steps
    )


def test_runner_rewrite_path_issues_new_subquery() -> None:
    llm = ScriptedLLM(
        [
            '{"sub_queries": ["bad"], "rationale": "x"}',  # plan
            '{"decision": "rewrite", "reason": "no hits"}',  # reflect iter 1
            '{"sub_query": "good", "rationale": "better"}',  # rewrite
            '{"decision": "answer", "reason": "ok"}',  # reflect iter 2 (after re-retrieve)
            "Answer.",  # synthesize
        ]
    )
    searcher = FakeSearcher(
        [
            [],  # first attempt: no hits
            [_make_hit("c1")],  # second attempt: hits
        ]
    )
    runner = AgentRunner(searcher=searcher, llm_call=llm, max_iterations=5)  # type: ignore[arg-type]
    result = runner.run(
        "q",
        user_context={"user_id": "u1"},
    )
    # The retriever was called twice (once for `bad`, once for `good`).
    assert searcher.calls == ["bad", "good"]


def test_runner_reflect_failure_defaults_to_answer() -> None:
    llm = ScriptedLLM(
        [
            '{"sub_queries": ["only"], "rationale": "x"}',
            "garbage reflect response",
            "Answer.",
        ]
    )
    searcher = FakeSearcher([[_make_hit("c1")]])
    runner = AgentRunner(searcher=searcher, llm_call=llm, max_iterations=3)  # type: ignore[arg-type]
    result = runner.run("q", user_context={"user_id": "u1"})
    assert result.answer == "Answer."


def test_runner_rejects_empty_query() -> None:
    searcher = FakeSearcher()
    runner = AgentRunner(searcher=searcher)  # type: ignore[arg-type]
    with pytest.raises(AgentRunnerError):
        runner.run("   ", user_context={"user_id": "u1"})


def test_runner_rejects_invalid_max_iterations() -> None:
    with pytest.raises(AgentRunnerError):
        AgentRunner(searcher=FakeSearcher(), max_iterations=0)  # type: ignore[arg-type]


def test_runner_propagates_retrieve_failure() -> None:
    class BrokenSearcher:
        def two_stage_search(self, query: str, *, acl_filter=None):
            raise RuntimeError("qdrant down")

    llm = ScriptedLLM(
        [
            '{"sub_queries": ["only"], "rationale": "x"}',
        ]
    )
    runner = AgentRunner(searcher=BrokenSearcher(), llm_call=llm, max_iterations=3)  # type: ignore[arg-type]
    with pytest.raises(AgentRunnerError) as exc:
        runner.run("q", user_context={"user_id": "u1"})
    assert "retrieve failed" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# LangGraph compilation
# ---------------------------------------------------------------------------


def test_build_agent_graph_compiles() -> None:
    searcher = FakeSearcher([[_make_hit("c1")]])
    tool = CachedRetrievalTool(searcher)  # type: ignore[arg-type]
    llm = ScriptedLLM(["unused"])
    g = build_agent_graph(llm_call=llm, retrieval_tool=tool)
    # The compiled object exposes `.invoke(state)` and `.stream(state)`.
    assert hasattr(g, "invoke")
    assert hasattr(g, "stream")


# ---------------------------------------------------------------------------
# run_retrieval helper (top-level convenience)
# ---------------------------------------------------------------------------


def test_run_retrieval_dedupes_parents_and_children() -> None:
    parent = _make_hit("p1", score=0.9)
    child = _make_hit("c1", score=0.7)
    duplicate = _make_hit("p1", score=0.5)

    class Stub:
        def two_stage_search(self, query: str, *, acl_filter=None):
            return TwoStageResult(
                parents=[parent, duplicate],
                children=[child],
                fallback_triggered=False,
                top_score=0.9,
            )

    hits = run_retrieval(Stub(), "q")
    chunk_ids = [h.chunk_id for h in hits]
    assert chunk_ids == ["p1", "c1"]


# ---------------------------------------------------------------------------
# Step 10 / 2026-09-05 — default_agent_llm_call Settings injection.
# ---------------------------------------------------------------------------


def test_default_agent_llm_call_threads_settings(monkeypatch) -> None:
    """Agent path applies Settings.agent_synth_* when caller passes no kwargs.

    Default: thinking ON (multi-hop benefits from reasoning) +
    max_tokens=1024. The function must NOT inject extra_body when
    `agent_synth_enable_thinking` is True (the default) and must
    inject max_tokens=1024 only when the caller didn't override.
    """
    from agentic_rag_project.config import get_settings
    from agentic_rag_project.agent_core.runner import default_agent_llm_call

    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    def _fake_completion(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [
                {"message": {"content": "ok", "role": "assistant"}},
            ],
        }

    # Monkeypatch the symbol the runner module already imported.
    import agentic_rag_project.agent_core.runner as _runner_mod

    monkeypatch.setattr(_runner_mod, "completion_with_metrics", _fake_completion)

    default_agent_llm_call("sys", "user", 30.0)

    # Default agent_synth_enable_thinking=True → no extra_body injected.
    assert "extra_body" not in captured, (
        "thinking ON must not inject extra_body={enable_thinking: False}"
    )
    # max_tokens injected from Settings default.
    assert captured.get("max_tokens") == 1024
    # timeout + model + messages still flow through.
    assert captured.get("timeout") == pytest.approx(30.0)
    assert captured.get("model") == get_settings().litellm_model
    assert len(captured.get("messages", [])) == 2