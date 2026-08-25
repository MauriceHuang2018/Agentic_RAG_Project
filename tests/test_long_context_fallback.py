"""Tests for the long-context fallback path (T3.3).

Covers:
  * build_long_context_prompt renders parents + history + query
  * build_long_context_prompt truncates when budget exceeded
  * build_long_context_prompt stops once budget is exhausted
  * LongContextFallback.generate prepends [long-context] tag
  * LongContextFallback honors custom model name
  * LongContextFallback rejects empty query / no parents
  * LongContextFallback rejects invalid constructor args
  * LongContextResult.to_metadata has the expected shape
"""

from __future__ import annotations

from typing import Any

import pytest

from agentic_rag_project.retrieval_direct.long_context_fallback import (
    LONG_CONTEXT_TAG,
    LongContextFallback,
    LongContextFallbackError,
    LongContextResult,
    build_long_context_prompt,
)
from agentic_rag_project.retrieval_direct.search import SearchResult


def _make_parent(chunk_id: str, content: str) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=content,
        score=0.0,
        payload={"is_parent": True, "chunk_id": chunk_id},
    )


# ---------------------------------------------------------------------------
# build_long_context_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_parents_query_history() -> None:
    parents = [
        _make_parent("p1", "first section content"),
        _make_parent("p2", "second section content"),
    ]
    history = [
        {"role": "user", "content": "earlier q"},
        {"role": "assistant", "content": "earlier a"},
    ]
    prompt, truncated = build_long_context_prompt(
        "the question", parents, history=history
    )
    assert "first section content" in prompt
    assert "second section content" in prompt
    assert "the question" in prompt
    assert "[earlier q]" in prompt or "[user]" in prompt.lower()
    assert "[p1]" in prompt
    assert "[p2]" in prompt
    assert truncated is False


def test_build_prompt_truncates_when_budget_exceeded() -> None:
    # p1 fits (~104 chars), p2 & p3 don't.
    parents = [
        _make_parent("p1", "a" * 100),
        _make_parent("p2", "b" * 500),
        _make_parent("p3", "c" * 500),
    ]
    prompt, truncated = build_long_context_prompt(
        "q", parents, max_parent_chars=120
    )
    assert truncated is True
    assert "[p1]" in prompt
    # p2's block exceeds remaining budget → not appended at all.
    assert "\n[p2]\n" not in prompt
    assert "\n[p3]\n" not in prompt


def test_build_prompt_stops_iterating_when_budget_zero() -> None:
    parents = [_make_parent(f"p{i}", "x" * 100) for i in range(10)]
    prompt, truncated = build_long_context_prompt(
        "q", parents, max_parent_chars=150
    )
    assert truncated is True
    # Only the first parent fits.
    assert "[p0]" in prompt
    # No later parent should appear as a section header. Check the
    # line-start to avoid matching `[p1]` as a substring of `[p10]`.
    for i in range(1, 10):
        assert f"\n[p{i}]\n" not in prompt


def test_build_prompt_empty_history_is_fine() -> None:
    prompt, _ = build_long_context_prompt(
        "q", [_make_parent("p1", "body")], history=None
    )
    assert "body" in prompt
    assert "q" in prompt


# ---------------------------------------------------------------------------
# LongContextFallback.generate
# ---------------------------------------------------------------------------


def test_generate_prepends_long_context_tag() -> None:
    called = {"n": 0}

    def fake_llm(system: str, user: str, timeout: float) -> str:
        called["n"] += 1
        assert system  # system prompt provided
        return "Answer text."

    fb = LongContextFallback(llm_call=fake_llm, model="fake-model")
    result = fb.generate("q", [_make_parent("p1", "x")])
    assert called["n"] == 1
    assert result.answer.startswith(LONG_CONTEXT_TAG)
    assert "Answer text." in result.answer
    assert result.model == "fake-model"
    assert result.parents_used == ["p1"]
    assert result.truncated is False


def test_generate_does_not_double_tag() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: f"{LONG_CONTEXT_TAG} already-tagged",
        model="m",
    )
    result = fb.generate("q", [_make_parent("p1", "x")])
    assert result.answer.count(LONG_CONTEXT_TAG) == 1


def test_generate_caps_answer_length() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: "x" * 5000,
        model="m",
        max_answer_chars=200,
    )
    result = fb.generate("q", [_make_parent("p1", "x")])
    # Tag prefix adds ~16 chars; the body cap is 200, so the body
    # before the tag is 200 chars.
    assert len(result.answer.split(" ", 1)[1]) == 200


def test_generate_truncated_flag_propagates() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: "answer",
        model="m",
        max_parent_chars=100,
    )
    parents = [
        _make_parent("p1", "a" * 200),
        _make_parent("p2", "b" * 200),
    ]
    result = fb.generate("q", parents)
    assert result.truncated is True


def test_generate_rejects_empty_query() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: "x", model="m"
    )
    with pytest.raises(LongContextFallbackError):
        fb.generate("   ", [_make_parent("p1", "x")])


def test_generate_rejects_no_parents() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: "x", model="m"
    )
    with pytest.raises(LongContextFallbackError):
        fb.generate("q", [])


def test_generate_rejects_empty_llm_response() -> None:
    fb = LongContextFallback(
        llm_call=lambda s, u, t: "",
        model="m",
    )
    with pytest.raises(LongContextFallbackError) as exc:
        fb.generate("q", [_make_parent("p1", "x")])
    assert "empty" in str(exc.value).lower()


def test_generate_uses_settings_model_when_unset() -> None:
    captured: dict[str, Any] = {}

    def fake_llm(system: str, user: str, timeout: float) -> str:
        return "answer"

    fb = LongContextFallback(llm_call=fake_llm)  # model=None
    result = fb.generate("q", [_make_parent("p1", "x")])
    # Default `litellm_long_context_model` is `claude-sonnet-4-5`.
    assert result.model != ""
    assert isinstance(result.model, str)


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_invalid_max_parent_chars() -> None:
    with pytest.raises(LongContextFallbackError):
        LongContextFallback(llm_call=lambda s, u, t: "x", max_parent_chars=0)


def test_constructor_rejects_invalid_max_answer_chars() -> None:
    with pytest.raises(LongContextFallbackError):
        LongContextFallback(llm_call=lambda s, u, t: "x", max_answer_chars=0)


def test_constructor_rejects_invalid_timeout() -> None:
    with pytest.raises(LongContextFallbackError):
        LongContextFallback(llm_call=lambda s, u, t: "x", timeout_seconds=0)


# ---------------------------------------------------------------------------
# LongContextResult
# ---------------------------------------------------------------------------


def test_long_context_result_to_metadata_shape() -> None:
    r = LongContextResult(
        answer="x", parents_used=["p1", "p2"], model="m", truncated=True
    )
    md = r.to_metadata()
    assert md == {
        "source": "long-context",
        "model": "m",
        "parents_used": ["p1", "p2"],
        "truncated": True,
    }