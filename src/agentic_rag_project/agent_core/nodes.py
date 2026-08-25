"""Graph node functions for the agentic workflow (T3.2).

Each node is a pure function: `(state, deps) -> partial_state_update`.
LangGraph merges the returned dict into the state. Dependencies
(retriever, llm_call, calculator, …) are passed in via the
`AgentRunner`, not looked up globally, so every node is testable in
isolation.

Node responsibilities:

  * `plan_node`        — decompose the user query into 1–N sub-queries,
                         or rewrite the current sub-query when reflect
                         asked for `rewrite`.
  * `retrieve_node`    — call the (cached) retriever with the current
                         sub-query; append hits to `retrieved_chunks`.
  * `reflect_node`     — ask the LLM whether we have enough context
                         (`answer`), need another loop (`continue`),
                         or should rewrite the sub-query (`rewrite`).
  * `synthesize_node`  — produce the final answer with citations
                         using the accumulated `retrieved_chunks` and
                         chat history.

Each node appends one `AgentStep` to `state["steps"]` for audit.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from typing import Any, Callable

from agentic_rag_project.agent_core.state import (
    AgentStep,
    ReflectDecision,
    append_step,
)
from agentic_rag_project.agent_core.tools import CachedRetrievalTool
from agentic_rag_project.retrieval_direct.search import SearchResult

logger = logging.getLogger(__name__)


# Type alias for the injected LLM callable used by plan/reflect/synthesize.
LLMCallFn = Callable[[str, str, float], str]
"""Signature: (system_prompt, user_prompt, timeout_seconds) -> raw text."""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = (
    "You are a planner for a multi-hop RAG agent. Decompose the user's "
    "question into an ordered list of focused sub-queries that, when "
    "answered in sequence, give enough context to synthesize the final "
    "answer. Output strict JSON: "
    '{"sub_queries": ["...","..."], "rationale": "<one sentence>"}\n'
    "Keep the list short (1–4 items) and use the user's original "
    "language. If the question is already a single lookup, return a "
    "single-item list containing the original query verbatim."
)

REWRITE_SYSTEM_PROMPT = (
    "You are re-planning one step of a multi-hop RAG agent. The previous "
    "sub-query returned no useful retrieval. Produce ONE alternative "
    "sub-query that is more likely to retrieve relevant documents while "
    "still serving the original user goal. Output strict JSON: "
    '{"sub_query": "...", "rationale": "<one sentence>"}\n'
    "Stay in the user's original language."
)

REFLECT_SYSTEM_PROMPT = (
    "You are a reflector for a multi-hop RAG agent. Given the user's "
    "original question, the chat history, the list of sub-queries already "
    "issued, and the retrieved context chunks so far, decide whether to "
    "(a) issue another sub-query, (b) rewrite the last sub-query, or "
    "(c) synthesize the final answer now.\n\n"
    "Output strict JSON: "
    '{"decision": "continue"|"rewrite"|"answer", '
    '"reason": "<one sentence>"}\n\n'
    "Default to 'answer' if the existing context already covers the "
    "question; default to 'continue' only if there is a clear gap."
)

SYNTHESIZE_SYSTEM_PROMPT = (
    "You are the final-answer synthesizer for a multi-hop RAG agent. "
    "Given the user's question, chat history, and the retrieved context "
    "chunks (each with a chunk_id), produce a concise answer in the "
    "user's language. Every claim must cite the chunk ids you used, "
    "formatted as `[chunk_id]` inline next to the claim. If the context "
    "is insufficient, say so explicitly rather than fabricating. Output "
    "the answer text only — no JSON, no preamble."
)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict | None:
    """Find the first balanced JSON object in `text`.

    Tolerates models that wrap JSON in ```json fences or add a leading
    sentence before the JSON block.
    """
    text = text.strip()
    # Strip code fences.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Find the first '{' and try to parse from there.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def _llm_json(llm_call: LLMCallFn, system: str, user: str, timeout: float) -> dict:
    """Call the LLM and parse a JSON object out of the response."""
    raw = llm_call(system, user, timeout)
    parsed = _extract_json(raw)
    if parsed is None:
        raise ValueError(f"agent: llm returned no parseable json: {raw!r}")
    return parsed


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def plan_node(
    state: dict,
    *,
    llm_call: LLMCallFn,
    timeout: float,
) -> dict:
    """Decompose the user query into sub-queries, or rewrite the next one.

    On the first iteration: produce `sub_queries` from scratch.
    On subsequent iterations: keep the existing list and let reflect
    drive whether to `continue` (advance index) or `rewrite` (replace
    the current sub-query via `rewrite_node`).
    """
    start = time.monotonic()
    iteration = int(state.get("iteration", 0))
    sub_queries: list[str] = list(state.get("sub_queries", []))
    existing_index = int(state.get("current_sub_query_index", 0))

    if not sub_queries:
        user_prompt = (
            f"User question: {state['query']}\n"
            f"Conversation history (most recent last): "
            f"{json.dumps(state.get('history', []), ensure_ascii=False)[:1500]}\n"
        )
        try:
            payload = _llm_json(llm_call, PLAN_SYSTEM_PROMPT, user_prompt, timeout)
            raw_list = payload.get("sub_queries", [])
        except Exception as exc:
            logger.warning("plan_node: llm failed, falling back: %s", exc)
            raw_list = []
            payload = {}
        if isinstance(raw_list, list) and raw_list:
            sub_queries = [str(s) for s in raw_list if str(s).strip()][:4]
        fallback_used = False
        if not sub_queries:
            # Fall back to the original query verbatim if the LLM
            # returned nothing usable — never run with an empty plan.
            sub_queries = [str(state["query"]).strip()]
            fallback_used = True
        rationale = str(payload.get("rationale", ""))[:512]
        if fallback_used:
            action = "plan fallback — using original query as single sub-query"
        else:
            action = f"plan produced {len(sub_queries)} sub-queries"
        detail = {
            "sub_queries": sub_queries,
            "rationale": rationale,
            "fallback": fallback_used,
        }
    else:
        # Re-plan was requested by reflect — bump the index past
        # completed work and let the caller issue the next sub-query.
        action = "plan advanced to next sub-query"
        detail = {"completed_index": existing_index}

    step = AgentStep(
        iteration=iteration,
        node="plan",
        action=action,
        detail=detail,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    update: dict = {"sub_queries": sub_queries}
    append_step(state, step)  # type: ignore[arg-type]
    state["steps"] = state.get("steps", [])  # noop; langgraph reads it
    return update


def rewrite_node(
    state: dict,
    *,
    llm_call: LLMCallFn,
    timeout: float,
) -> dict:
    """Replace the current sub-query with one that is more likely to retrieve."""
    start = time.monotonic()
    iteration = int(state.get("iteration", 0))
    sub_queries: list[str] = list(state.get("sub_queries", []))
    idx = int(state.get("current_sub_query_index", 0))

    user_prompt = (
        f"Original question: {state['query']}\n"
        f"Last sub-query (which failed to retrieve): "
        f"{sub_queries[idx] if idx < len(sub_queries) else ''}\n"
        "Produce a single alternative sub-query."
    )
    payload = _llm_json(llm_call, REWRITE_SYSTEM_PROMPT, user_prompt, timeout)
    new_sub_query = str(payload.get("sub_query", "")).strip()
    if not new_sub_query:
        new_sub_query = str(state["query"]).strip()
    if idx < len(sub_queries):
        sub_queries[idx] = new_sub_query
    else:
        sub_queries.append(new_sub_query)

    step = AgentStep(
        iteration=iteration,
        node="plan",
        action="plan rewrote the current sub-query",
        detail={
            "index": idx,
            "new_sub_query": new_sub_query,
            "rationale": str(payload.get("rationale", ""))[:512],
        },
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    append_step(state, step)  # type: ignore[arg-type]
    return {"sub_queries": sub_queries}


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


def retrieve_node(
    state: dict,
    *,
    retrieval_tool: CachedRetrievalTool,
    acl_filter: Any | None,
) -> dict:
    """Run the retriever for the current sub-query and accumulate chunks."""
    start = time.monotonic()
    iteration = int(state.get("iteration", 0))
    sub_queries: list[str] = list(state.get("sub_queries", []))
    idx = int(state.get("current_sub_query_index", 0))

    if not sub_queries:
        # Defensive — should not happen because plan_node seeds the list.
        sub_query = str(state.get("query", "")).strip()
    elif idx < len(sub_queries):
        sub_query = sub_queries[idx]
    else:
        # Past the end of the plan — synthesize instead.
        return {"reflect_decision": ReflectDecision.ANSWER.value}

    hits: list[SearchResult] = retrieval_tool.run(sub_query, acl_filter=acl_filter)
    new_chunks: list[dict] = list(state.get("retrieved_chunks", []))
    new_ids: list[str] = list(state.get("retrieved_chunk_ids", []))
    for hit in hits:
        new_chunks.append(asdict(hit))
        if hit.chunk_id not in new_ids:
            new_ids.append(hit.chunk_id)

    fallback = bool(retrieval_tool.fallback_triggered)

    step = AgentStep(
        iteration=iteration,
        node="retrieve",
        action=f"retrieved {len(hits)} chunks for sub-query",
        detail={
            "sub_query": sub_query,
            "hits": len(hits),
            "fallback_triggered": fallback,
        },
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    append_step(state, step)  # type: ignore[arg-type]
    return {
        "retrieved_chunks": new_chunks,
        "retrieved_chunk_ids": new_ids,
        "fallback_triggered": fallback or bool(state.get("fallback_triggered", False)),
        "current_sub_query_index": idx + 1,
    }


# ---------------------------------------------------------------------------
# reflect
# ---------------------------------------------------------------------------


def reflect_node(
    state: dict,
    *,
    llm_call: LLMCallFn,
    timeout: float,
) -> dict:
    """Ask the LLM whether to continue, rewrite, or answer."""
    start = time.monotonic()
    iteration = int(state.get("iteration", 0))
    sub_queries: list[str] = list(state.get("sub_queries", []))
    idx = int(state.get("current_sub_query_index", 0))

    user_prompt = (
        f"Original question: {state['query']}\n"
        f"Chat history: {json.dumps(state.get('history', []), ensure_ascii=False)[:800]}\n"
        f"Sub-queries issued so far: {json.dumps(sub_queries, ensure_ascii=False)}\n"
        f"Current sub-query index: {idx}\n"
        f"Retrieved chunks count: {len(state.get('retrieved_chunks', []))}\n"
    )
    try:
        payload = _llm_json(llm_call, REFLECT_SYSTEM_PROMPT, user_prompt, timeout)
        decision_raw = str(payload.get("decision", "answer")).strip().lower()
    except Exception as exc:
        logger.warning("reflect_node: llm failed, defaulting to answer: %s", exc)
        decision_raw = ReflectDecision.ANSWER.value
        payload = {"decision": decision_raw, "reason": "llm failure"}
    try:
        decision = ReflectDecision(decision_raw)
    except ValueError:
        decision = ReflectDecision.ANSWER

    step = AgentStep(
        iteration=iteration,
        node="reflect",
        action=f"reflect decided: {decision.value}",
        detail={
            "decision": decision.value,
            "reason": str(payload.get("reason", ""))[:512],
        },
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    append_step(state, step)  # type: ignore[arg-type]
    return {
        "reflect_decision": decision.value,
        "reflection": str(payload.get("reason", ""))[:512],
    }


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


def synthesize_node(
    state: dict,
    *,
    llm_call: LLMCallFn,
    timeout: float,
    max_answer_chars: int = 4000,
) -> dict:
    """Generate the final answer (and citations) from accumulated context."""
    start = time.monotonic()
    iteration = int(state.get("iteration", 0))

    chunks = state.get("retrieved_chunks", [])
    chunks_block = "\n\n".join(
        f"[{c.get('chunk_id', '?')}] {c.get('content', '')[:600]}"
        for c in chunks[:20]
    )
    user_prompt = (
        f"User question: {state['query']}\n"
        f"Chat history: {json.dumps(state.get('history', []), ensure_ascii=False)[:800]}\n"
        f"Retrieved context:\n{chunks_block}\n"
        "Answer with `[chunk_id]` citations."
    )
    raw_answer = llm_call(SYNTHESIZE_SYSTEM_PROMPT, user_prompt, timeout) or ""
    final_answer = raw_answer.strip()[:max_answer_chars]
    cited_ids = re.findall(r"\[([a-zA-Z0-9_:\-]+)\]", final_answer)
    seen: set[str] = set()
    citations: list[dict] = []
    for cid in cited_ids:
        if cid in seen:
            continue
        seen.add(cid)
        # Hydrate a minimal SearchResult stub — real conversion happens
        # in the runner when it returns AgentResult.
        citations.append({"chunk_id": cid, "document_id": "", "score": 0.0, "content": ""})

    step = AgentStep(
        iteration=iteration,
        node="synthesize",
        action="synthesized final answer",
        detail={
            "answer_chars": len(final_answer),
            "cited_chunk_ids": sorted(seen),
        },
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    append_step(state, step)  # type: ignore[arg-type]
    return {"final_answer": final_answer, "citations": citations}


# ---------------------------------------------------------------------------
# termination guard
# ---------------------------------------------------------------------------


def should_continue(state: dict) -> bool:
    """Edge predicate: keep iterating while there are sub-queries left
    and we haven't hit the max-iteration cap."""
    iteration = int(state.get("iteration", 0))
    max_iter = int(state.get("max_iterations", 5))
    if iteration >= max_iter:
        return False
    decision = state.get("reflect_decision", "")
    if decision == ReflectDecision.ANSWER.value:
        return False
    idx = int(state.get("current_sub_query_index", 0))
    sub_queries = list(state.get("sub_queries", []))
    if idx >= len(sub_queries):
        return False
    return True