"""Agent-graph L1 metrics (T4.3).

`agent_node_total{node_name, outcome}` and
`agent_node_duration_seconds{node_name}` — the two levers to
spot a slow plan/retrieve/reflect/synthesize node in a single
Grafana panel.

`record_agent_node(node_name, duration_s, outcome)` is the
single helper called from each node wrapper.
"""

from __future__ import annotations

from agentic_rag_project.observability.registry import get_metrics


def record_agent_node(
    *,
    node_name: str,
    duration_s: float,
    outcome: str = "ok",
) -> None:
    """Record one node invocation.

    `node_name` ∈ {"plan", "retrieve", "reflect", "synthesize"}.
    `outcome` ∈ {"ok", "error", "skipped"}.
    """
    if duration_s < 0:
        duration_s = 0.0
    metrics = get_metrics()
    metrics.agent_node_total.labels(
        node_name=node_name, outcome=outcome
    ).inc()
    metrics.agent_node_duration_seconds.labels(node_name=node_name).observe(
        duration_s
    )


__all__ = ["record_agent_node"]
