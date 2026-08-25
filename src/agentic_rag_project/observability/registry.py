"""Prometheus metrics registry (T4.3).

DESIGN 4.7 — single source of truth for every metric the
prototype exposes at `/metrics`. Layered in three tiers:

  L1 — request-path metrics (counter / histogram). Incremented
       inline in the request hot path. Cheap; safe to fire on
       every request.

  L2 — evaluation-quality gauges. Refreshed by the Celery beat
       `refresh_eval_gauges` task from `evaluation_results`.
       Updates are O(N workspaces × M metric names).

  L3 — business-derived gauges. Refreshed by
       `refresh_business_gauges`. Reads `feedbacks`, `tickets`,
       `citations` — these are aggregations, so off the hot path.

Why a singleton registry? `prometheus_client` raises
`ValueError: Duplicated timeseries` if the same metric is
defined twice in the same Python process. The chat service,
feedback service, and retrieval layer each import a *helper*
that reads the metric from this registry — never recreates it.

Multiprocess: in production gunicorn runs multiple workers.
We pin `PROMETHEUS_MULTIPROC_DIR` in the env and use
`multiprocess.MultiProcessCollector` in the middleware. The
singleton uses `prometheus_client`'s default `CollectorRegistry`
in single-process mode (tests) and `MultiProcessCollector`
otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
)


# ---------------------------------------------------------------------------
# Histogram buckets
# ---------------------------------------------------------------------------

# Latency buckets — cover direct (ms) up to agentic fallback (10s+).
# 0.05s … 0.1s … 0.25s … 0.5s … 1s … 2.5s … 5s … 10s … 30s
_LATENCY_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

# Sub-second latency buckets — for retrieval & agent nodes (often < 1s).
_FAST_BUCKETS = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
)


@dataclass(frozen=True)
class MetricsRegistry:
    """All 25 metrics defined up-front, grouped by layer."""

    # ----- L1 — request-path counters -----
    chat_requests_total: Counter
    chat_tokens_total: Counter
    retrieval_requests_total: Counter
    feedback_total: Counter
    long_context_fallback_total: Counter
    post_processor_redactions_total: Counter
    embedding_cache_hits_total: Counter
    agent_node_total: Counter
    # Drift detector (T4.4) — hot-path counter, incremented once per
    # detected drift event by `drift.detector.DriftDetector.run()`.
    drift_events_total: Counter

    # ----- L1 — request-path histograms -----
    chat_latency_seconds: Histogram
    retrieval_latency_seconds: Histogram
    agent_node_duration_seconds: Histogram

    # ----- L1 — request-path gauges -----
    chat_active_sessions: Gauge

    # ----- L2 — evaluation-quality gauges (offline refresh) -----
    eval_faithfulness_score: Gauge
    eval_citation_accuracy: Gauge
    eval_context_recall: Gauge
    eval_context_precision: Gauge
    eval_answer_relevance: Gauge
    eval_hallucination_rate: Gauge

    # ----- L3 — business-derived gauges (offline refresh) -----
    csat_score: Gauge
    kb_activation_rate: Gauge
    feedback_dislike_rate: Gauge
    dislike_attribution_count: Gauge
    open_tickets_by_status: Gauge


def build_registry(registry: CollectorRegistry | None = None) -> MetricsRegistry:
    """Build the metric set against the given registry.

    Pass a fresh registry per test to avoid duplicate-registration
    errors. Production callers pass `None` (the global default).
    """
    reg = registry or CollectorRegistry()

    return MetricsRegistry(
        # ----- L1 counters -----
        chat_requests_total=Counter(
            "chat_requests_total",
            "Total chat requests handled, partitioned by route + status.",
            labelnames=(
                "workspace_id",
                "router_class",
                "fallback_used",
                "status",
            ),
            registry=reg,
        ),
        chat_tokens_total=Counter(
            "chat_tokens_total",
            "Total tokens consumed, partitioned by model + direction.",
            labelnames=("model", "direction"),
            registry=reg,
        ),
        retrieval_requests_total=Counter(
            "retrieval_requests_total",
            "Total retrieval calls, partitioned by retriever + top_k.",
            labelnames=("retriever", "top_k"),
            registry=reg,
        ),
        feedback_total=Counter(
            "feedback_total",
            "Total feedback submissions, partitioned by rating + outcome.",
            labelnames=("rating", "attribution_status", "category_key"),
            registry=reg,
        ),
        long_context_fallback_total=Counter(
            "long_context_fallback_total",
            "How often the long-context fallback fired, by model.",
            labelnames=("model",),
            registry=reg,
        ),
        post_processor_redactions_total=Counter(
            "post_processor_redactions_total",
            "Sensitive / PII redactions applied, partitioned by rule_id.",
            labelnames=("rule_id",),
            registry=reg,
        ),
        embedding_cache_hits_total=Counter(
            "embedding_cache_hits_total",
            "Embedding cache hits, partitioned by cache_type (dense/sparse).",
            labelnames=("cache_type",),
            registry=reg,
        ),
        agent_node_total=Counter(
            "agent_node_total",
            "LangGraph node invocations, partitioned by node_name + outcome.",
            labelnames=("node_name", "outcome"),
            registry=reg,
        ),
        drift_events_total=Counter(
            "drift_events_total",
            "Drift events detected by the 15-min Celery beat task "
            "(T4.4 drift_detector), partitioned by kind / metric / "
            "severity / scope.",
            labelnames=("kind", "metric_name", "severity", "scope"),
            registry=reg,
        ),
        # ----- L1 histograms -----
        chat_latency_seconds=Histogram(
            "chat_latency_seconds",
            "End-to-end chat latency (seconds).",
            labelnames=("router_class", "fallback_used"),
            buckets=_LATENCY_BUCKETS,
            registry=reg,
        ),
        retrieval_latency_seconds=Histogram(
            "retrieval_latency_seconds",
            "Retrieval call latency (seconds).",
            labelnames=("retriever", "top_k"),
            buckets=_FAST_BUCKETS,
            registry=reg,
        ),
        agent_node_duration_seconds=Histogram(
            "agent_node_duration_seconds",
            "Per-node duration inside the LangGraph agent.",
            labelnames=("node_name",),
            buckets=_FAST_BUCKETS,
            registry=reg,
        ),
        # ----- L1 gauges -----
        chat_active_sessions=Gauge(
            "chat_active_sessions",
            "Number of in-flight chat requests at this instant.",
            registry=reg,
        ),
        # ----- L2 evaluation gauges -----
        eval_faithfulness_score=Gauge(
            "eval_faithfulness_score",
            "Rolling mean faithfulness from evaluation_results (T4.1).",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        eval_citation_accuracy=Gauge(
            "eval_citation_accuracy",
            "Rolling mean citation_accuracy from evaluation_results.",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        eval_context_recall=Gauge(
            "eval_context_recall",
            "Rolling mean context_recall from evaluation_results.",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        eval_context_precision=Gauge(
            "eval_context_precision",
            "Rolling mean context_precision from evaluation_results.",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        eval_answer_relevance=Gauge(
            "eval_answer_relevance",
            "Rolling mean answer_relevance from evaluation_results.",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        eval_hallucination_rate=Gauge(
            "eval_hallucination_rate",
            "1 - eval_faithfulness_score; enterprise risk metric.",
            labelnames=("workspace_id", "eval_set"),
            registry=reg,
        ),
        # ----- L3 business gauges -----
        csat_score=Gauge(
            "csat_score",
            "Rolling 7-day user satisfaction: like / (like + dislike).",
            labelnames=("workspace_id",),
            registry=reg,
        ),
        kb_activation_rate=Gauge(
            "kb_activation_rate",
            "Distinct chunks cited / total kb chunks over 7 days.",
            labelnames=("workspace_id",),
            registry=reg,
        ),
        feedback_dislike_rate=Gauge(
            "feedback_dislike_rate",
            "Dislike / (like + dislike) over 7 days.",
            labelnames=("workspace_id", "category_key"),
            registry=reg,
        ),
        dislike_attribution_count=Gauge(
            "dislike_attribution_count",
            "Count of dislike rows per attribution category (7d window).",
            labelnames=("category_key",),
            registry=reg,
        ),
        open_tickets_by_status=Gauge(
            "open_tickets_by_status",
            "Open ticket count per ticket_status key.",
            labelnames=("status",),
            registry=reg,
        ),
    )


# ---------------------------------------------------------------------------
# Singleton + multiprocess glue
# ---------------------------------------------------------------------------


def _use_multiprocess() -> bool:
    """True iff we're under gunicorn (env var set by supervisord)."""
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))


def get_default_registry() -> CollectorRegistry:
    """Return a fresh multiprocess collector in prod, default in tests."""
    if _use_multiprocess():
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        return reg
    return CollectorRegistry()


# Cached singleton — lazy-built on first import. Tests should call
# `reset_default_registry()` between cases to avoid duplicate-
# registration errors when they instantiate their own app.
_default_registry: CollectorRegistry | None = None
_default_metrics: MetricsRegistry | None = None


def get_metrics() -> MetricsRegistry:
    """Lazy accessor for the singleton metric set."""
    global _default_registry, _default_metrics
    if _default_metrics is None:
        _default_registry = get_default_registry()
        _default_metrics = build_registry(_default_registry)
    return _default_metrics


def reset_default_registry() -> None:
    """Drop the singleton — for tests only."""
    global _default_registry, _default_metrics
    _default_registry = None
    _default_metrics = None


__all__ = [
    "MetricsRegistry",
    "build_registry",
    "get_default_registry",
    "get_metrics",
    "reset_default_registry",
]
