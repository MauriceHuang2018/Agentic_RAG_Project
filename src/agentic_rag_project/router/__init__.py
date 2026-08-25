"""Question router — classifies a query into `direct` vs `agent` routes (T3.1).

DESIGN 4.2 / TASK T3.1. The classifier is a conservative LLM + keyword
ensemble: when in doubt we route to the agent graph because under-routing
shows up as a silent missing citation, while over-routing only costs
extra LLM calls.
"""

from agentic_rag_project.router.classifier import (
    CLASSIFY_SYSTEM_PROMPT,
    DEFAULT_AGENT_CONFIDENCE_THRESHOLD,
    DEFAULT_AGENT_KEYWORDS,
    ROUTE_AGENT,
    ROUTE_DIRECT,
    ClassifierError,
    ConfidenceRouter,
    KeywordClassifier,
    LLMClassifier,
    RouteDecision,
    collect_keyword_hits,
    default_litellm_call,
)

__all__ = [
    "CLASSIFY_SYSTEM_PROMPT",
    "ClassifierError",
    "ConfidenceRouter",
    "DEFAULT_AGENT_CONFIDENCE_THRESHOLD",
    "DEFAULT_AGENT_KEYWORDS",
    "KeywordClassifier",
    "LLMClassifier",
    "ROUTE_AGENT",
    "ROUTE_DIRECT",
    "RouteDecision",
    "collect_keyword_hits",
    "default_litellm_call",
]