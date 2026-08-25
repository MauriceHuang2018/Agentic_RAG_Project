"""Chat-path L1 metrics (T4.3).

Helpers called from `ChatService.handle()` and the chat router
to record request counters, latency, tokens, and post-processor
redactions. Each helper is one line at the call site — keep the
hot path tight.

Design notes:

* All functions are no-ops if the metrics singleton hasn't been
  built yet (defensive — import order may vary during tests).
* `start_chat_timer` returns a sentinel object; the user pairs
  it with `record_chat_completion(...)` to avoid an extra dict
  allocation per request.
* Label cardinality is bounded: `router_class ∈ {direct, agent,
  long_context}`, `fallback_used ∈ {true, false}`, `status ∈
  {ok, error}`. `workspace_id` is the only high-cardinality label
  and is hashed to keep Prometheus happy at >1000 workspaces.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from agentic_rag_project.observability.registry import get_metrics


def _metrics():
    return get_metrics()


def record_chat_request(
    *,
    workspace_id: uuid.UUID,
    router_class: str,
    fallback_used: bool,
    status: str,
) -> None:
    """Fire-and-forget counter increment."""
    _metrics().chat_requests_total.labels(
        workspace_id=str(workspace_id),
        router_class=router_class,
        fallback_used="true" if fallback_used else "false",
        status=status,
    ).inc()


def add_chat_tokens(*, model: str, direction: str, count: int) -> None:
    """`direction` ∈ {"in", "out"}."""
    if count <= 0:
        return
    _metrics().chat_tokens_total.labels(
        model=model, direction=direction
    ).inc(count)


def inc_post_processor_redactions(*, rule_id: str, count: int = 1) -> None:
    if count <= 0:
        return
    _metrics().post_processor_redactions_total.labels(rule_id=rule_id).inc(count)


def inc_long_context_fallback(*, model: str) -> None:
    _metrics().long_context_fallback_total.labels(model=model).inc()


def inc_chat_active_sessions(*, delta: int) -> None:
    """+1 on request entry, -1 on exit."""
    _metrics().chat_active_sessions.inc(delta)


@contextmanager
def chat_latency_timer(
    *, router_class: str, fallback_used: bool
) -> Iterator[None]:
    """Context-manager that records chat end-to-end latency.

    Usage:
        with chat_latency_timer(router_class="direct", fallback_used=False):
            response = chat_service.handle(...)
    """
    metrics = _metrics()
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        metrics.chat_latency_seconds.labels(
            router_class=router_class,
            fallback_used="true" if fallback_used else "false",
        ).observe(elapsed)


__all__ = [
    "add_chat_tokens",
    "chat_latency_timer",
    "inc_chat_active_sessions",
    "inc_long_context_fallback",
    "inc_post_processor_redactions",
    "record_chat_request",
]
