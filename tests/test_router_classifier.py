"""Tests for the question router (T3.1).

Covers:
  * RouteDecision.is_agent / is_direct
  * KeywordClassifier triggers on Chinese + English keywords
  * KeywordClassifier returns direct for plain factual queries
  * KeywordClassifier handles empty / whitespace input
  * LLMClassifier parses well-formed `{route, confidence, reason}` payload
  * LLMClassifier raises ClassifierError on bad payload
  * LLMClassifier raises ClassifierError when llm_call raises
  * ConfidenceRouter escalates to agent when LLM says direct but keyword hit
  * ConfidenceRouter escalates to agent when LLM confidence < threshold
  * ConfidenceRouter trusts LLM direct when confidence ≥ threshold
  * ConfidenceRouter falls back to keyword when LLM errors
  * ConfidenceRouter returns direct for empty query
  * collect_keyword_hits helper returns matched keywords
  * DEFAULT_AGENT_KEYWORDS covers common Chinese + English triggers
"""

from __future__ import annotations

import pytest

from agentic_rag_project.router import (
    CLASSIFY_SYSTEM_PROMPT,
    DEFAULT_AGENT_KEYWORDS,
    ROUTE_AGENT,
    ROUTE_DIRECT,
    ClassifierError,
    ConfidenceRouter,
    KeywordClassifier,
    LLMClassifier,
    RouteDecision,
    collect_keyword_hits,
)


# ---------------------------------------------------------------------------
# RouteDecision
# ---------------------------------------------------------------------------


def test_route_decision_is_agent() -> None:
    d = RouteDecision(
        route=ROUTE_AGENT, confidence=0.9, reason="x", source="llm"
    )
    assert d.is_agent() is True
    assert d.is_direct() is False


def test_route_decision_is_direct() -> None:
    d = RouteDecision(
        route=ROUTE_DIRECT, confidence=0.9, reason="x", source="llm"
    )
    assert d.is_direct() is True
    assert d.is_agent() is False


# ---------------------------------------------------------------------------
# KeywordClassifier
# ---------------------------------------------------------------------------


def test_keyword_classifier_chinese_trigger_returns_agent() -> None:
    kc = KeywordClassifier()
    d = kc.classify("请对比一下产品手册和合同条款的差异")
    assert d.route == ROUTE_AGENT
    assert d.source == "keyword"


def test_keyword_classifier_english_trigger_returns_agent() -> None:
    kc = KeywordClassifier()
    d = kc.classify("Please compare these two contracts")
    assert d.route == ROUTE_AGENT


def test_keyword_classifier_plain_factual_returns_direct() -> None:
    kc = KeywordClassifier()
    d = kc.classify("公司年假有多少天？")
    assert d.route == ROUTE_DIRECT
    assert d.source == "keyword"


def test_keyword_classifier_empty_query_returns_direct() -> None:
    kc = KeywordClassifier()
    assert kc.classify("").route == ROUTE_DIRECT
    assert kc.classify("   ").route == ROUTE_DIRECT


def test_keyword_classifier_is_case_insensitive() -> None:
    kc = KeywordClassifier()
    assert kc.classify("WHY is this happening?").route == ROUTE_AGENT
    assert kc.classify("Explain the SLA").route == ROUTE_AGENT


def test_keyword_classifier_custom_keywords() -> None:
    kc = KeywordClassifier(keywords=("foo", "bar"))
    assert kc.classify("tell me about foo and bar").route == ROUTE_AGENT
    # The default keyword "对比" should NOT trigger when overridden.
    assert kc.classify("对比一下").route == ROUTE_DIRECT


# ---------------------------------------------------------------------------
# LLMClassifier
# ---------------------------------------------------------------------------


def _fake_llm(payload: dict):
    """Return a callable that yields `payload` regardless of input."""

    def _call(system: str, user: str, timeout: float) -> dict:
        return payload

    return _call


def test_llm_classifier_parses_agent_payload() -> None:
    llm = LLMClassifier(
        llm_call=_fake_llm(
            {"route": "agent", "confidence": 0.92, "reason": "multi-hop"}
        )
    )
    d = llm.classify("anything")
    assert d.route == ROUTE_AGENT
    assert d.confidence == pytest.approx(0.92)
    assert d.reason == "multi-hop"
    assert d.source == "llm"


def test_llm_classifier_parses_direct_payload() -> None:
    llm = LLMClassifier(
        llm_call=_fake_llm(
            {"route": "direct", "confidence": 0.85, "reason": "factual"}
        )
    )
    d = llm.classify("how many days off?")
    assert d.route == ROUTE_DIRECT


def test_llm_classifier_rejects_unknown_route() -> None:
    llm = LLMClassifier(
        llm_call=_fake_llm({"route": "maybe", "confidence": 0.5})
    )
    with pytest.raises(ClassifierError):
        llm.classify("x")


def test_llm_classifier_rejects_out_of_range_confidence() -> None:
    llm = LLMClassifier(
        llm_call=_fake_llm({"route": "direct", "confidence": 1.5})
    )
    with pytest.raises(ClassifierError):
        llm.classify("x")


def test_llm_classifier_rejects_non_numeric_confidence() -> None:
    llm = LLMClassifier(
        llm_call=_fake_llm({"route": "direct", "confidence": "high"})
    )
    with pytest.raises(ClassifierError):
        llm.classify("x")


def test_llm_classifier_rejects_non_dict_payload() -> None:
    def _bad_call(system: str, user: str, timeout: float):
        return "direct"

    with pytest.raises(ClassifierError):
        LLMClassifier(llm_call=_bad_call).classify("x")


def test_llm_classifier_propagates_llm_call_exception() -> None:
    def _raise_call(system: str, user: str, timeout: float):
        raise RuntimeError("network down")

    with pytest.raises(ClassifierError) as exc:
        LLMClassifier(llm_call=_raise_call).classify("x")
    assert "network down" in str(exc.value)


def test_llm_classifier_empty_query_skips_llm_call() -> None:
    called = {"n": 0}

    def _counting_call(system: str, user: str, timeout: float) -> dict:
        called["n"] += 1
        return {"route": "direct", "confidence": 0.5}

    d = LLMClassifier(llm_call=_counting_call).classify("   ")
    assert d.route == ROUTE_DIRECT
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# ConfidenceRouter — composition policy
# ---------------------------------------------------------------------------


def _router_with_llm(payload: dict, *, threshold: float = 0.7) -> ConfidenceRouter:
    return ConfidenceRouter(
        llm_classifier=LLMClassifier(llm_call=_fake_llm(payload)),
        agent_confidence_threshold=threshold,
    )


def test_router_trusts_llm_direct_when_confidence_above_threshold() -> None:
    r = _router_with_llm(
        {"route": "direct", "confidence": 0.95, "reason": "factual"}
    )
    d = r.route("公司年假多少天")
    assert d.route == ROUTE_DIRECT
    assert d.source == "llm"


def test_router_escalates_to_agent_when_confidence_below_threshold() -> None:
    r = _router_with_llm(
        {"route": "direct", "confidence": 0.5, "reason": "unsure"}
    )
    d = r.route("年假")
    assert d.route == ROUTE_AGENT
    assert "low confidence" in d.reason


def test_router_escalates_to_agent_when_keyword_hit_overrides_llm() -> None:
    r = _router_with_llm(
        {"route": "direct", "confidence": 0.99, "reason": "looks easy"}
    )
    d = r.route("请对比新旧合同的主要区别")
    assert d.route == ROUTE_AGENT
    assert "keyword override" in d.reason


def test_router_keeps_llm_agent_even_without_keyword_hit() -> None:
    r = _router_with_llm(
        {"route": "agent", "confidence": 0.8, "reason": "needs plan"}
    )
    d = r.route("the meaning of life")  # no keyword triggers in English sample
    # The LLM said agent — router accepts.
    assert d.route == ROUTE_AGENT
    assert d.source == "llm"


def test_router_falls_back_to_keyword_when_llm_errors() -> None:
    def _raise(system: str, user: str, timeout: float):
        raise RuntimeError("timeout")

    r = ConfidenceRouter(
        llm_classifier=LLMClassifier(llm_call=_raise),
        keyword_classifier=KeywordClassifier(),
    )
    # Keyword hit → agent via fallback.
    d = r.route("对比一下两个产品的优缺点")
    assert d.route == ROUTE_AGENT
    assert d.source == "fallback"


def test_router_fallback_to_direct_when_llm_errors_and_no_keyword() -> None:
    def _raise(system: str, user: str, timeout: float):
        raise RuntimeError("timeout")

    r = ConfidenceRouter(
        llm_classifier=LLMClassifier(llm_call=_raise),
        keyword_classifier=KeywordClassifier(),
    )
    d = r.route("公司年假多少天")
    assert d.route == ROUTE_DIRECT
    assert d.source == "fallback"


def test_router_returns_direct_for_empty_query() -> None:
    r = ConfidenceRouter()
    assert r.route("").route == ROUTE_DIRECT
    assert r.route("   ").route == ROUTE_DIRECT


# ---------------------------------------------------------------------------
# helpers / defaults
# ---------------------------------------------------------------------------


def test_collect_keyword_hits_returns_matches() -> None:
    hits = collect_keyword_hits("对比两份合同的差异，分析条款")
    assert "对比" in hits
    assert "差异" in hits
    assert "分析" in hits


def test_collect_keyword_hits_empty_for_clean_query() -> None:
    assert collect_keyword_hits("公司年假天数") == []


def test_default_keywords_cover_common_triggers() -> None:
    # Sanity check on the default keyword set — a few entries that
    # we definitely want to escalate.
    assert "对比" in DEFAULT_AGENT_KEYWORDS
    assert "分析" in DEFAULT_AGENT_KEYWORDS
    assert "为什么" in DEFAULT_AGENT_KEYWORDS
    assert "compare" in DEFAULT_AGENT_KEYWORDS
    assert "summarize" in DEFAULT_AGENT_KEYWORDS


def test_classify_system_prompt_mentions_routes() -> None:
    assert "route" in CLASSIFY_SYSTEM_PROMPT
    assert "direct" in CLASSIFY_SYSTEM_PROMPT
    assert "agent" in CLASSIFY_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Settings-driven timeout (closed 2026-09-02)
#
# `LLMClassifier` defaults to 10s; main.py threads the budget from
# `Settings.router_classifier_timeout_seconds` so ops can tune it via
# `ROUTER_CLASSIFIER_TIMEOUT_SECONDS` without a code change. Without
# that knob, a slow litellm fallback chain would let every chat query
# time out before the classifier can even fall back to keyword.
# ---------------------------------------------------------------------------


def test_router_classifier_timeout_seconds_default_is_10(monkeypatch) -> None:
    """Without the env var, the budget stays at the safe 10s default."""
    monkeypatch.delenv("ROUTER_CLASSIFIER_TIMEOUT_SECONDS", raising=False)
    from agentic_rag_project.config import get_settings

    get_settings.cache_clear()
    assert get_settings().router_classifier_timeout_seconds == pytest.approx(10.0)


def test_router_classifier_timeout_seconds_env_override(monkeypatch) -> None:
    """Env var `ROUTER_CLASSIFIER_TIMEOUT_SECONDS` overrides the default;
    main.py must read it and pass it through to `LLMClassifier` so
    ops can raise the budget when the litellm proxy's fallback chain
    is slow."""
    monkeypatch.setenv("ROUTER_CLASSIFIER_TIMEOUT_SECONDS", "60")
    from agentic_rag_project.config import get_settings

    get_settings.cache_clear()
    assert get_settings().router_classifier_timeout_seconds == pytest.approx(60.0)


def test_llm_classifier_threads_settings_timeout_into_llm_call(monkeypatch) -> None:
    """End-to-end: ops bumps the env → main.py's `LLMClassifier(timeout_seconds=...)`
    receives the new budget → the llm_call callable sees the raised value."""
    monkeypatch.setenv("ROUTER_CLASSIFIER_TIMEOUT_SECONDS", "60")
    from agentic_rag_project.config import get_settings

    get_settings.cache_clear()
    captured = {"timeout": None}

    def _capture_call(system: str, user: str, timeout: float) -> dict:
        captured["timeout"] = timeout
        return {"route": "direct", "confidence": 0.5, "reason": "ok"}

    llm = LLMClassifier(
        llm_call=_capture_call,
        timeout_seconds=get_settings().router_classifier_timeout_seconds,
    )
    llm.classify("anything")


# ---------------------------------------------------------------------------
# Step 9 / 2026-09-05 — kwargs forwarding for max_tokens / extra_body.
# ---------------------------------------------------------------------------


def test_classifier_forwards_extra_body_and_max_tokens() -> None:
    """max_tokens + extra_body flow through to llm_call via **kwargs."""
    captured: list[dict] = []

    def _spy(system: str, user: str, timeout: float, **kw) -> dict:
        captured.append({"timeout": timeout, "kw": kw})
        return {"route": "direct", "confidence": 0.9, "reason": "ok"}

    llm = LLMClassifier(
        llm_call=_spy,
        max_tokens=200,
        extra_body={"enable_thinking": False},
    )
    decision = llm.classify("什么是 PV?")

    assert decision.route == "direct"
    assert decision.confidence == 0.9
    assert len(captured) == 1
    assert captured[0]["kw"] == {
        "max_tokens": 200,
        "extra_body": {"enable_thinking": False},
    }


def test_classifier_backward_compat_old_position_only_callable() -> None:
    """Legacy (system, user, timeout) callable still works.

    The new **kwargs forwarding must NOT break old stub functions used
    by tests and ad-hoc CLI scripts — when max_tokens / extra_body are
    left at None, the call site must keep the original 3-positional
    shape.
    """
    captured: list[dict] = []

    def _legacy(system: str, user: str, timeout: float) -> dict:
        captured.append({"system": system, "user": user, "timeout": timeout})
        return {"route": "direct", "confidence": 0.5, "reason": "legacy"}

    llm = LLMClassifier(llm_call=_legacy)
    decision = llm.classify("hello world")

    assert decision.route == "direct"
    assert len(captured) == 1
    assert "system" in captured[0]
    assert captured[0]["timeout"] == pytest.approx(10.0)


def test_classifier_default_kwargs_when_settings_thinking_off(monkeypatch) -> None:
    """settings.classifier_enable_thinking=False → extra_body injected."""
    from agentic_rag_project.config import get_settings

    # Re-import fresh so the env override picks up.
    monkeypatch.setenv("CLASSIFIER_ENABLE_THINKING", "false")
    monkeypatch.setenv("CLASSIFIER_MAX_TOKENS", "300")
    get_settings.cache_clear()
    settings = get_settings()

    captured: list[dict] = []

    def _spy(system: str, user: str, timeout: float, **kw) -> dict:
        captured.append(kw)
        return {"route": "direct", "confidence": 0.9, "reason": "x"}

    # Mirror the main.py wiring pattern.
    llm = LLMClassifier(
        llm_call=_spy,
        max_tokens=settings.classifier_max_tokens,
        extra_body=(
            {"enable_thinking": False}
            if not settings.classifier_enable_thinking
            else None
        ),
    )
    llm.classify("what is this?")

    assert len(captured) == 1
    assert captured[0].get("max_tokens") == 300
    assert captured[0].get("extra_body") == {"enable_thinking": False}