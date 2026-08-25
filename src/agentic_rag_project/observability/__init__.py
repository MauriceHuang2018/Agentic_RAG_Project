"""Prometheus observability (T4.3).

Public surface:

  * `get_metrics()` — singleton accessor for the 25-metric set
  * `reset_default_registry()` — drop the singleton (tests only)
  * `MetricsAllowlist`, `build_metrics_router` — `/metrics` HTTP
    endpoint factory with IP allowlist
  * `refresh_eval_gauges`, `refresh_business_gauges` — Celery
    beat tasks (L2 / L3)
  * Request-path helpers:
      - `record_chat_request`, `chat_latency_timer`,
        `add_chat_tokens`, `inc_chat_active_sessions`,
        `inc_long_context_fallback`,
        `inc_post_processor_redactions` (chat_metrics)
      - `record_feedback` (feedback_metrics)
      - `retrieval_latency_timer`, `inc_embedding_cache_hit`
        (retrieval_metrics)
      - `record_agent_node` (agent_metrics)

Mount `/metrics` from your FastAPI app via:

    from agentic_rag_project.observability import build_metrics_router
    app.include_router(build_metrics_router(registry=REGISTRY))

Schedule L2/L3 refreshes from Celery beat:

    from agentic_rag_project.observability.collector import (
        make_refresh_eval_task, make_refresh_business_task,
    )
    refresh_eval = make_refresh_eval_task()
    refresh_business = make_refresh_business_task()
"""

from agentic_rag_project.observability.agent_metrics import record_agent_node
from agentic_rag_project.observability.chat_metrics import (
    add_chat_tokens,
    chat_latency_timer,
    inc_chat_active_sessions,
    inc_long_context_fallback,
    inc_post_processor_redactions,
    record_chat_request,
)
from agentic_rag_project.observability.collector import (
    DEFAULT_WINDOW_DAYS,
    make_refresh_business_task,
    make_refresh_eval_task,
    refresh_business_gauges,
    refresh_eval_gauges,
)
from agentic_rag_project.observability.feedback_metrics import record_feedback
from agentic_rag_project.observability.middleware import (
    MetricsAllowlist,
    build_metrics_router,
)
from agentic_rag_project.observability.registry import (
    MetricsRegistry,
    build_registry,
    get_default_registry,
    get_metrics,
    reset_default_registry,
)
from agentic_rag_project.observability.retrieval_metrics import (
    inc_embedding_cache_hit,
    retrieval_latency_timer,
)

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MetricsAllowlist",
    "MetricsRegistry",
    "add_chat_tokens",
    "build_metrics_router",
    "build_registry",
    "chat_latency_timer",
    "get_default_registry",
    "get_metrics",
    "inc_chat_active_sessions",
    "inc_embedding_cache_hit",
    "inc_long_context_fallback",
    "inc_post_processor_redactions",
    "make_refresh_business_task",
    "make_refresh_eval_task",
    "record_agent_node",
    "record_chat_request",
    "record_feedback",
    "refresh_business_gauges",
    "refresh_eval_gauges",
    "reset_default_registry",
    "retrieval_latency_timer",
]
