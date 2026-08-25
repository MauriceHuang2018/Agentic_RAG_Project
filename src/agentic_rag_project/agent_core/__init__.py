"""Multi-hop agent core (T3.2).

DESIGN 6.2 — `plan → retrieve → reflect → (loop or synthesize)`.

Public surface:

  * `AgentRunner` — pure-Python loop runner used by the chat endpoint
    and tests.
  * `build_agent_graph` — compiles the same nodes into a LangGraph
    `StateGraph` for production use.
  * `AgentState` / `AgentStep` / `AgentResult` — the data shapes that
    travel across node boundaries.
  * `run_calculator`, `run_web_search`, `run_retrieval`,
    `CachedRetrievalTool` — the tool functions called from the
    retrieve node.

Long-context fallback (T3.3) is layered on top of `AgentResult` in
the chat endpoint, not inside the agent loop itself — see
`retrieval_direct.long_context_fallback`.
"""

from agentic_rag_project.agent_core.graph import build_agent_graph
from agentic_rag_project.agent_core.runner import (
    AgentRunner,
    AgentRunnerError,
    RetrievalBackend,
    default_agent_llm_call,
)
from agentic_rag_project.agent_core.state import (
    DEFAULT_MAX_ITERATIONS,
    AgentResult,
    AgentState,
    AgentStep,
    ReflectDecision,
    make_initial_state,
)
from agentic_rag_project.agent_core.tools import (
    CachedRetrievalTool,
    RetrievalFn,
    collect_fallback_signal,
    run_calculator,
    run_retrieval,
    run_web_search,
    search_results_to_state_payload,
)

__all__ = [
    "AgentResult",
    "AgentRunner",
    "AgentRunnerError",
    "AgentState",
    "AgentStep",
    "CachedRetrievalTool",
    "DEFAULT_MAX_ITERATIONS",
    "ReflectDecision",
    "RetrievalBackend",
    "RetrievalFn",
    "build_agent_graph",
    "collect_fallback_signal",
    "default_agent_llm_call",
    "make_initial_state",
    "run_calculator",
    "run_retrieval",
    "run_web_search",
    "search_results_to_state_payload",
]