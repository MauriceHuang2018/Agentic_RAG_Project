"""Agent runner — executes the agent graph without depending on
LangGraph's compiled `StateGraph` machinery (T3.2).

DESIGN 6.2 calls for a LangGraph `StateGraph` for the multi-hop
agent path. We model each node as a plain function so the runner
itself can be tested without spinning up the langgraph runtime;
`build_agent_graph` (in `agent_core.graph`) compiles those same
functions into a real `StateGraph` for the chat endpoint to invoke.

The runner here is the simpler, deterministic control loop we use
in tests and as a fallback when the langgraph runtime is not
available (e.g. inside a Celery task). Production should call
`build_agent_graph().invoke(state)` via the graph module.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any, Callable, Protocol

from agentic_rag_project.agent_core.nodes import (
    LLMCallFn,
    plan_node,
    reflect_node,
    retrieve_node,
    rewrite_node,
    should_continue,
    synthesize_node,
)
from agentic_rag_project.agent_core.state import (
    AgentResult,
    AgentState,
    DEFAULT_MAX_ITERATIONS,
    ReflectDecision,
    make_initial_state,
)
from agentic_rag_project.agent_core.tools import CachedRetrievalTool
from agentic_rag_project.observability.agent_metrics import record_agent_node
from agentic_rag_project.observability.llm_metrics import completion_with_metrics
from agentic_rag_project.retrieval_direct.search import SearchResult

logger = logging.getLogger(__name__)


def _timed_node(node_name: str, fn: Callable[..., dict], *args, **kwargs):
    """Run a node and record its L1 agent metrics.

    Any exception is swallowed by the underlying node (which logs and
    returns a sensible fallback); we therefore can't distinguish
    "ok after retry" from "error" without re-running, so we report
    "ok" on return. The rare unhandled exception bubbles up and is
    reported by the caller via `record_agent_node(outcome="error")`.
    """
    started = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
    except Exception:
        duration = time.perf_counter() - started
        record_agent_node(
            node_name=node_name, duration_s=duration, outcome="error"
        )
        raise
    duration = time.perf_counter() - started
    record_agent_node(node_name=node_name, duration_s=duration, outcome="ok")
    return result


class AgentRunnerError(Exception):
    """Raised when the runner cannot produce a final answer."""


class RetrievalBackend(Protocol):
    """Anything exposing `two_stage_search(query, *, acl_filter)`."""

    def two_stage_search(
        self, query: str, *, acl_filter: Any | None = None
    ) -> Any: ...


# Default factory for the LLM call. Imported lazily so the module can be
# imported without forcing litellm to load at import time.
def default_agent_llm_call(
    system_prompt: str, user_prompt: str, timeout: float, **kwargs: Any
) -> str:
    """Production LLM call via litellm.

    Returns the raw text content. We do NOT enforce JSON here because
    the synthesize node is allowed to return free-form text; the plan
    / reflect / rewrite nodes extract JSON with the tolerant
    `_extract_json` helper.

    Per-call kwargs (`max_tokens`, `extra_body`, ...) win over
    `Settings.agent_synth_*` so future per-node overrides (plan vs
    synth) can be wired by passing kwargs from `nodes.py` without
    changing this function. When the caller passes nothing, we read
    `Settings.agent_synth_*` defaults: thinking ON (multi-hop benefits
    from reasoning) + max_tokens=1024 (cap answer length).

    See Step 7 / 2026-09-05 (chat synth latency optimization).
    """
    from agentic_rag_project.config import get_settings

    settings = get_settings()
    if "max_tokens" not in kwargs and settings.agent_synth_max_tokens:
        kwargs["max_tokens"] = settings.agent_synth_max_tokens
    if "extra_body" not in kwargs and not settings.agent_synth_enable_thinking:
        kwargs["extra_body"] = {"enable_thinking": False}
    response = completion_with_metrics(
        model=settings.litellm_model,
        api_base=settings.litellm_base_url or None,
        api_key=settings.litellm_api_key or None,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=timeout,
        **kwargs,
    )
    return response["choices"][0]["message"]["content"] or ""


class AgentRunner:
    """Execute one agent turn end-to-end.

    All dependencies (LLM callable, retriever, ACL filter builder,
    timeouts) are injected so tests can substitute fakes without
    patching globals.
    """

    def __init__(
        self,
        *,
        searcher: RetrievalBackend,
        llm_call: LLMCallFn | None = None,
        acl_filter: Any | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        llm_timeout_seconds: float = 30.0,
    ) -> None:
        if max_iterations <= 0:
            raise AgentRunnerError("max_iterations must be positive")
        self._searcher = searcher
        self._llm_call: LLMCallFn = llm_call or default_agent_llm_call
        self._acl_filter = acl_filter
        self._max_iterations = max_iterations
        self._llm_timeout = llm_timeout_seconds

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        *,
        user_context: dict[str, Any],
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        acl_filter: Any | None = None,
    ) -> AgentResult:
        """Execute one agent turn.

        `acl_filter` is a per-call override of the runner's
        constructor-set ``self._acl_filter``. When the chat router
        builds a fresh `qmodels.Filter` per request (which is
        correct — workspace memberships and ACL rows change
        between calls) the caller passes it here so the retrieve
        node scopes Qdrant queries to the caller's permissions.
        Without this override the runner would silently reuse the
        filter from its construction time, which can leak across
        sessions. P0 / 2026-09-03.
        """
        if not (query or "").strip():
            raise AgentRunnerError("query must be non-empty")

        # Per-call wins over the constructor default. Treat None as
        # "use the default"; treat any truthy value as the new filter.
        effective_acl_filter = acl_filter if acl_filter is not None else self._acl_filter

        state = make_initial_state(
            query=query,
            user_context=user_context,
            conversation_id=conversation_id,
            history=history,
            max_iterations=self._max_iterations,
        )
        retrieval_tool = CachedRetrievalTool(self._searcher)
        truncated = False

        # First plan is unconditional so we always have sub-queries.
        try:
            plan_update = _timed_node(
                "plan",
                plan_node,
                state,
                llm_call=self._llm_call,
                timeout=self._llm_timeout,
            )
        except Exception as exc:
            logger.warning("agent plan failed, falling back to single-shot: %s", exc)
            plan_update = {"sub_queries": [query]}
            state.setdefault("steps", []).append(
                {
                    "node": "plan",
                    "action": "fallback to single sub-query (plan llm failed)",
                    "detail": {"error": str(exc)[:256]},
                    "iteration": 0,
                    "step_id": "",
                    "started_at": "",
                    "duration_ms": 0,
                }
            )
        _merge(state, plan_update)

        # Loop: retrieve → reflect → (continue | rewrite | answer).
        while int(state.get("iteration", 0)) < self._max_iterations:
            state["iteration"] = int(state.get("iteration", 0)) + 1
            try:
                r_update = _timed_node(
                    "retrieve",
                    retrieve_node,
                    state,
                    retrieval_tool=retrieval_tool,
                    acl_filter=effective_acl_filter,
                )
            except Exception as exc:
                logger.exception("agent retrieve failed")
                raise AgentRunnerError(f"retrieve failed: {exc}") from exc
            _merge(state, r_update)

            try:
                ref_update = _timed_node(
                    "reflect",
                    reflect_node,
                    state,
                    llm_call=self._llm_call,
                    timeout=self._llm_timeout,
                )
            except Exception as exc:
                logger.warning("agent reflect failed, defaulting to answer: %s", exc)
                _merge(
                    state,
                    {
                        "reflect_decision": ReflectDecision.ANSWER.value,
                        "reflection": "reflect failed — answering with current context",
                    },
                )
            else:
                _merge(state, ref_update)

            if state.get("reflect_decision") == ReflectDecision.REWRITE.value:
                # Back up the index BEFORE calling rewrite_node so it
                # overwrites the failed sub-query instead of appending
                # a new one to the end of the plan.
                state["current_sub_query_index"] = max(
                    0, int(state.get("current_sub_query_index", 0)) - 1
                )
                try:
                    rw_update = _timed_node(
                        "rewrite",
                        rewrite_node,
                        state,
                        llm_call=self._llm_call,
                        timeout=self._llm_timeout,
                    )
                    _merge(state, rw_update)
                except Exception as exc:
                    logger.warning("agent rewrite failed: %s", exc)
                    # Drop back to direct answer path.
                    _merge(state, {"reflect_decision": ReflectDecision.ANSWER.value})

            if not should_continue(state):
                break

        if int(state.get("iteration", 0)) >= self._max_iterations:
            truncated = True
            logger.info(
                "agent hit max_iterations=%d, synthesizing with partial context",
                self._max_iterations,
            )

        # Synthesize.
        try:
            s_update = _timed_node(
                "synthesize",
                synthesize_node,
                state,
                llm_call=self._llm_call,
                timeout=self._llm_timeout,
            )
        except Exception as exc:
            logger.exception("agent synthesize failed")
            raise AgentRunnerError(f"synthesize failed: {exc}") from exc
        _merge(state, s_update)

        # Materialize AgentResult.
        chunk_dicts = list(state.get("retrieved_chunks", []))
        citations = [_search_result_from_dict(d) for d in chunk_dicts]
        steps = list(state.get("steps", []))
        return AgentResult(
            answer=str(state.get("final_answer", "")),
            citations=citations,
            steps=[_step_from_dict(d) for d in steps],
            iterations=int(state.get("iteration", 0)),
            fallback_triggered=bool(state.get("fallback_triggered", False)),
            truncated_by_max_iter=truncated,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _merge(state: AgentState, update: dict[str, Any]) -> None:
    """In-place merge of a partial state update.

    LangGraph's `StateGraph` reducer does this automatically; the
    runner needs to do it by hand.
    """
    for k, v in update.items():
        state[k] = v


def _search_result_from_dict(payload: dict[str, Any]) -> SearchResult:
    """Reverse `asdict(SearchResult)` without forcing payload to exist."""
    return SearchResult(
        chunk_id=str(payload.get("chunk_id", "")),
        document_id=str(payload.get("document_id", "")),
        content=str(payload.get("content", "")),
        score=float(payload.get("score", 0.0)),
        payload=dict(payload.get("payload", {})),
    )


def _step_from_dict(payload: dict[str, Any]):
    from agentic_rag_project.agent_core.state import AgentStep

    return AgentStep(
        step_id=str(payload.get("step_id", "")),
        iteration=int(payload.get("iteration", 0)),
        node=str(payload.get("node", "")),
        action=str(payload.get("action", "")),
        detail=dict(payload.get("detail", {})),
        duration_ms=int(payload.get("duration_ms", 0)),
    )