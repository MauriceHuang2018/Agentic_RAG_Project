"""LangGraph `StateGraph` composition (T3.2).

Wraps the plain node functions in `agent_core.nodes` into a compiled
`StateGraph` so production callers can do
`graph.invoke(initial_state)` and let langgraph handle the edge
plumbing.

The plain runner (`agent_core.runner.AgentRunner`) is the canonical
implementation — it is deterministic, easier to test, and avoids
langgraph runtime surprises. The graph here is the production
entry point and shares every node with the runner, so behavior is
identical.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from agentic_rag_project.agent_core.nodes import (
    LLMCallFn,
    plan_node,
    reflect_node,
    retrieve_node,
    should_continue,
    synthesize_node,
)
from agentic_rag_project.agent_core.tools import CachedRetrievalTool
from agentic_rag_project.observability.agent_metrics import record_agent_node

logger = logging.getLogger(__name__)


def _wrap_node(name: str, fn: Callable[..., dict]) -> Callable[..., dict]:
    """Wrap a node fn so each invocation emits an L1 agent metric."""

    def _inner(state: dict, *args, **kwargs) -> dict:
        started = time.perf_counter()
        try:
            result = fn(state, *args, **kwargs)
        except Exception:
            duration = time.perf_counter() - started
            record_agent_node(
                node_name=name, duration_s=duration, outcome="error"
            )
            raise
        duration = time.perf_counter() - started
        record_agent_node(node_name=name, duration_s=duration, outcome="ok")
        return result

    return _inner


def build_agent_graph(
    *,
    llm_call: LLMCallFn,
    retrieval_tool: CachedRetrievalTool,
    acl_filter: Any | None = None,
    timeout: float = 30.0,
    max_answer_chars: int = 4000,
) -> Any:
    """Compile a LangGraph `StateGraph` for the multi-hop agent path.

    Returns the compiled graph (object exposes `.invoke(state)`).
    Imports langgraph lazily so importing `agent_core` does not force
    the langgraph runtime to load.
    """
    from langgraph.graph import END, StateGraph

    def _plan(state: dict) -> dict:
        return plan_node(state, llm_call=llm_call, timeout=timeout)

    def _retrieve(state: dict) -> dict:
        return retrieve_node(
            state, retrieval_tool=retrieval_tool, acl_filter=acl_filter
        )

    def _reflect(state: dict) -> dict:
        return reflect_node(state, llm_call=llm_call, timeout=timeout)

    def _synth(state: dict) -> dict:
        return synthesize_node(
            state,
            llm_call=llm_call,
            timeout=timeout,
            max_answer_chars=max_answer_chars,
        )

    def _router(state: dict) -> str:
        return "synth" if not should_continue(state) else "retrieve"

    g = StateGraph(dict)
    g.add_node("plan", _wrap_node("plan", _plan))
    g.add_node("retrieve", _wrap_node("retrieve", _retrieve))
    g.add_node("reflect", _wrap_node("reflect", _reflect))
    g.add_node("synth", _wrap_node("synthesize", _synth))
    g.set_entry_point("plan")
    g.add_edge("plan", "retrieve")
    g.add_edge("retrieve", "reflect")
    g.add_conditional_edges(
        "reflect", _router, {"synth": "synth", "retrieve": "retrieve"}
    )
    g.add_edge("synth", END)
    return g.compile()