"""Question router — direct (single-shot RAG) vs agentic (LangGraph).

DESIGN 4.2 / TASK T3.1: classify a query into one of two routes.

  * `direct` — a single retrieval + LLM completion is sufficient.
  * `agent`  — the query needs plan/retrieve/reflect loops (multi-hop,
    comparison, summary of multiple docs).

Strategy is intentionally conservative: when in doubt we choose
`agent` because under-routing shows up as a missing citation in
production (silent failure), while over-routing just costs a few
extra LLM calls.

The classifier has three parts:

  1. `LLMClassifier`  — calls LiteLLM, parses `{route, confidence}`.
  2. `KeywordClassifier` — pure-Python rule on Chinese/English trigger
     words; used as fallback when the LLM is unavailable or returns
     an unparseable payload.
  3. `ConfidenceRouter` — fuses the two with the conservative policy:
        LLM says `agent`              → agent
        LLM says `direct`, conf < θ   → agent
        LLM says `direct`, conf ≥ θ   → direct
        keyword hit                   → agent
        LLM errored / unparseable     → fallback to keyword
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agentic_rag_project.observability.llm_metrics import completion_with_metrics

logger = logging.getLogger(__name__)


ROUTE_DIRECT = "direct"
ROUTE_AGENT = "agent"

# Keywords that almost always mean "needs plan+retrieve" rather than a
# one-shot RAG lookup. English keywords are lowercased before match;
# the matcher uses a case-insensitive substring test.
DEFAULT_AGENT_KEYWORDS: tuple[str, ...] = (
    # Chinese — reasoning / aggregation / multi-step
    "对比", "比较", "区别", "差异",
    "分析", "总结", "概括", "综述",
    "为什么", "为何", "原因", "解释",
    "怎么", "如何", "怎样", "方法", "步骤",
    "推导", "证明", "论证",
    "综合", "汇总", "归纳", "整理",
    "评估", "评价", "优劣",
    # English
    "compare", "comparison", "difference", "vs",
    "analyze", "analyse", "analysis",
    "summarize", "summary", "summarise", "synthesize", "synthesis",
    "why", "explain", "reason", "reasoning",
    "how to", "how can", "step by step",
    "evaluate", "assessment", "pros and cons",
)

DEFAULT_AGENT_CONFIDENCE_THRESHOLD = 0.7

# Cap on query length the LLM is asked to classify. Anything longer is
# truncated to keep the prompt cheap and predictable.
MAX_CLASSIFY_QUERY_LEN = 2000

CLASSIFY_SYSTEM_PROMPT = (
    "You are a query router for a RAG system. "
    "Decide whether the user's question needs a single retrieval pass "
    "(route='direct') or a multi-step plan-and-retrieve loop "
    "(route='agent'). Respond with strict JSON only, no commentary, "
    "matching this schema:\n"
    '{"route": "direct"|"agent", "confidence": 0.0-1.0, "reason": "<one short sentence>"}\n'
    "Use route='agent' when the question asks for comparison, "
    "multi-hop reasoning, summarization across several documents, "
    "or step-by-step reasoning. Use route='direct' for simple "
    "factual lookups."
)


class ClassifierError(Exception):
    """Raised when neither the LLM nor the keyword classifier can decide."""


@dataclass(frozen=True)
class RouteDecision:
    """The router's verdict for one query."""

    route: str  # 'direct' | 'agent'
    confidence: float
    reason: str
    source: str  # 'llm' | 'keyword' | 'fallback'

    def is_agent(self) -> bool:
        return self.route == ROUTE_AGENT

    def is_direct(self) -> bool:
        return self.route == ROUTE_DIRECT


# Type alias for the injected LLM callable. Tests substitute a fake
# here; production wires `litellm.completion`.
LLMCallFn = Callable[..., dict]
"""
Signature: (system_prompt, user_prompt, timeout_seconds, **kwargs) -> parsed dict.
The dict must contain `route` (`'direct'|'agent'`) and `confidence`
(`0.0..1.0`); `reason` is optional. `**kwargs` (max_tokens, extra_body,
...) are forwarded by `LLMClassifier` from `Settings.classifier_*`
knobs (wired in Step 6 / 2026-09-05) so call sites can cap output and
toggle reasoning without changing the signature.
"""


def default_litellm_call(
    system_prompt: str, user_prompt: str, timeout: float, **kwargs: Any
) -> dict:
    """Default LLM call wired to litellm.completion.

    Imports litellm lazily (inside `completion_with_metrics`) so the
    module can be imported by tests that never invoke the router
    (no network round-trip required at import time).

    Extra kwargs (`max_tokens`, `extra_body`, ...) flow through to
    `completion_with_metrics` unchanged. The caller (`LLMClassifier.classify`)
    assembles them from `Settings.classifier_*` knobs.
    """
    from agentic_rag_project.config import get_settings

    settings = get_settings()
    response = completion_with_metrics(
        model=settings.litellm_model,
        api_base=settings.litellm_base_url or None,
        api_key=settings.litellm_api_key or None,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=timeout,
        response_format={"type": "json_object"},
        **kwargs,
    )
    content = response["choices"][0]["message"]["content"]
    return json.loads(content)


@dataclass
class KeywordClassifier:
    """Pure-Python rule-based classifier.

    A hit on any keyword (case-insensitive substring) returns `agent`;
    otherwise `direct`. Used both as a fallback when the LLM fails
    and as a *bias* inside `ConfidenceRouter` — any keyword hit
    escalates the route to `agent` regardless of the LLM verdict.
    """

    keywords: tuple[str, ...] = DEFAULT_AGENT_KEYWORDS

    def classify(self, query: str) -> RouteDecision:
        normalized = query.strip().lower()
        if not normalized:
            return RouteDecision(
                route=ROUTE_DIRECT,
                confidence=0.5,
                reason="empty query — nothing to plan",
                source="keyword",
            )
        hits: list[str] = []
        for kw in self.keywords:
            needle = kw.lower()
            if needle in normalized:
                hits.append(kw)
        if hits:
            return RouteDecision(
                route=ROUTE_AGENT,
                confidence=0.9,
                reason=f"keyword triggers: {', '.join(hits[:3])}",
                source="keyword",
            )
        return RouteDecision(
            route=ROUTE_DIRECT,
            confidence=0.6,
            reason="no agent trigger keywords matched",
            source="keyword",
        )


@dataclass
class LLMClassifier:
    """Calls the LLM and parses `{route, confidence, reason}`.

    The `llm_call` parameter is the injection point: tests pass a
    deterministic function returning a fixed dict; production uses
    `default_litellm_call`.

    `max_tokens` and `extra_body` are forwarded as `**kwargs` to
    `llm_call` on every invocation. They default to `None` so existing
    callers (tests, single-call sites) keep the previous signature —
    only the production wiring in `main.ConfidenceRouter` sets them.
    See Step 6 / 2026-09-05 (chat synth latency optimization).
    """

    llm_call: LLMCallFn = default_litellm_call
    timeout_seconds: float = 10.0
    system_prompt: str = CLASSIFY_SYSTEM_PROMPT
    max_tokens: int | None = None
    extra_body: dict[str, Any] | None = None

    def classify(self, query: str) -> RouteDecision:
        q = query.strip()
        if not q:
            return RouteDecision(
                route=ROUTE_DIRECT,
                confidence=0.5,
                reason="empty query — nothing to plan",
                source="llm",
            )
        truncated = q[:MAX_CLASSIFY_QUERY_LEN]
        # Build kwargs only when the caller opted in — preserves the
        # legacy `llm_call(system, user, timeout)` signature for old
        # stubs that don't accept **kwargs.
        kwargs: dict[str, Any] = {}
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.extra_body is not None:
            kwargs["extra_body"] = self.extra_body
        try:
            payload = self.llm_call(
                self.system_prompt, truncated, self.timeout_seconds, **kwargs
            )
        except Exception as exc:  # pragma: no cover — only fires on real LLM
            raise ClassifierError(f"llm call failed: {exc}") from exc
        decision = _parse_llm_payload(payload)
        if decision is None:
            raise ClassifierError(
                f"llm returned unparseable payload: {payload!r}"
            )
        return decision


def _parse_llm_payload(payload: dict) -> RouteDecision | None:
    """Validate and normalize a dict from the LLM. Returns None on bad input."""
    if not isinstance(payload, dict):
        return None
    route = str(payload.get("route", "")).strip().lower()
    if route not in {ROUTE_DIRECT, ROUTE_AGENT}:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    reason = str(payload.get("reason", "")).strip() or "no reason given"
    return RouteDecision(
        route=route,
        confidence=confidence,
        reason=reason[:512],
        source="llm",
    )


@dataclass
class ConfidenceRouter:
    """Composes LLM + keyword classifiers with a conservative policy.

    Resolution order:

      1. Run keyword classifier; remember whether it hit.
      2. Run LLM classifier.
           * If LLM returns `agent` → `agent`.
           * If LLM returns `direct` with conf ≥ θ → `direct`
             (unless keyword hit, in which case → `agent`).
           * If LLM returns `direct` with conf < θ → `agent`.
      3. On LLM error, fall back to keyword verdict.

    The threshold default (0.7) lives in
    `DEFAULT_AGENT_CONFIDENCE_THRESHOLD` and can be overridden per
    instance.
    """

    llm_classifier: LLMClassifier = field(default_factory=LLMClassifier)
    keyword_classifier: KeywordClassifier = field(
        default_factory=KeywordClassifier
    )
    agent_confidence_threshold: float = DEFAULT_AGENT_CONFIDENCE_THRESHOLD

    def route(self, query: str) -> RouteDecision:
        q = query.strip()
        if not q:
            return RouteDecision(
                route=ROUTE_DIRECT,
                confidence=0.5,
                reason="empty query",
                source="fallback",
            )
        keyword_verdict = self.keyword_classifier.classify(q)
        keyword_hit = keyword_verdict.is_agent()
        try:
            llm_verdict = self.llm_classifier.classify(q)
        except ClassifierError as exc:
            logger.warning(
                "router: llm failed, falling back to keyword: %s", exc
            )
            return RouteDecision(
                route=keyword_verdict.route,
                confidence=keyword_verdict.confidence,
                reason=f"llm fallback — {keyword_verdict.reason}",
                source="fallback",
            )

        if llm_verdict.is_agent():
            return llm_verdict

        # LLM says direct. Decide whether to trust it.
        if keyword_hit:
            return RouteDecision(
                route=ROUTE_AGENT,
                confidence=max(llm_verdict.confidence, keyword_verdict.confidence),
                reason=(
                    f"keyword override ({keyword_verdict.reason}) "
                    f"despite llm direct ({llm_verdict.confidence:.2f})"
                ),
                source="llm",
            )
        if llm_verdict.confidence < self.agent_confidence_threshold:
            return RouteDecision(
                route=ROUTE_AGENT,
                confidence=llm_verdict.confidence,
                reason=(
                    f"low confidence ({llm_verdict.confidence:.2f} < "
                    f"{self.agent_confidence_threshold:.2f}) — escalate"
                ),
                source="llm",
            )
        return llm_verdict


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def collect_keyword_hits(
    query: str, keywords: Iterable[str] = DEFAULT_AGENT_KEYWORDS
) -> list[str]:
    """Return the list of trigger keywords present in `query`.

    Public utility for tests and for callers that want to surface the
    keyword bias alongside the LLM verdict (e.g. for debugging in
    audit logs).
    """
    normalized = query.lower()
    return [kw for kw in keywords if kw.lower() in normalized]


# Pre-compile keyword patterns if we ever need to switch from substring
# to whole-word matching; kept here so future maintenance is one spot.
_KEYWORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def compile_keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Return a whole-word regex for a keyword (cached)."""
    if keyword not in _KEYWORD_RE_CACHE:
        _KEYWORD_RE_CACHE[keyword] = re.compile(
            r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE
        )
    return _KEYWORD_RE_CACHE[keyword]