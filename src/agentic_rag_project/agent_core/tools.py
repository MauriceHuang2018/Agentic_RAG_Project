"""Tool functions used by the agent graph nodes (T3.2).

Three tools live here:

  * `run_retrieval`       — calls the two-stage retriever with an ACL
    filter built from the user context. Returns the SearchResults.
  * `run_calculator`      — evaluates a Python arithmetic expression
    in a sandboxed namespace (no builtins). Pure local; no network.
  * `run_web_search`      — *stub* that returns a deterministic fake
    response. See TODO at the bottom of this file for the production
    integration roadmap (company DB, vendor search APIs, etc).

All three are pure functions with no hidden state, so the nodes can
swap in fakes during testing without touching the rest of the graph.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from typing import Any, Callable, Protocol

from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.two_stage import TwoStageSearcher

logger = logging.getLogger(__name__)


class RetrievalFn(Protocol):
    """Shape the agent graph expects from a retriever — easy to fake."""

    def two_stage_search(
        self,
        query: str,
        *,
        acl_filter: Any | None = None,
    ) -> Any: ...  # TwoStageResult, kept loose to avoid import cycles


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


def run_retrieval(
    searcher: RetrievalFn,
    sub_query: str,
    *,
    acl_filter: Any | None = None,
) -> list[SearchResult]:
    """Run the two-stage retriever on one sub-query.

    Concatenates parent + child hits into a single ordered list, with
    parents first (so the synthesized answer can lead with high-level
    context) and children grouped by parent. Duplicates are dropped by
    chunk_id while preserving order.
    """
    result = searcher.two_stage_search(sub_query, acl_filter=acl_filter)
    seen: set[str] = set()
    out: list[SearchResult] = []
    for hit in list(getattr(result, "parents", [])) + list(
        getattr(result, "children", [])
    ):
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        out.append(hit)
    return out


def collect_fallback_signal(searcher: RetrievalFn, sub_query: str) -> bool:
    """Return True if the latest retrieval was a low-confidence fallback.

    The runner uses this flag to decide whether to invoke the
    long-context fallback path (T3.3). We only consult this after a
    retrieval has already run, so it's a thin wrapper that re-invokes
    `two_stage_search`; in the agent graph we cache the last result
    instead — see `CachedRetrievalTool` below.
    """
    result = searcher.two_stage_search(sub_query)
    return bool(getattr(result, "fallback_triggered", False))


# ---------------------------------------------------------------------------
# calculator (sandboxed arithmetic)
# ---------------------------------------------------------------------------


_CALC_ALLOWED = re.compile(r"^[\d\s+\-*/().,%]+$")


def run_calculator(expression: str) -> str:
    """Safely evaluate a math expression.

    Accepts digits, whitespace, and the operators `+ - * / ( ) % ,`.
    Rejects everything else (no variables, no function calls, no
    attribute access). Empty expressions return an empty string.

    SECURITY: `eval()` is used here by design — the calculator's job
    IS to evaluate expressions — but it is gated by three independent
    layers:

      1. `_CALC_ALLOWED` regex restricts the input character set to
         digits + arithmetic operators + parentheses + whitespace +
         `%` and `,` (for `2,000`-style group separators).
      2. `__builtins__` is replaced with an empty dict so no Python
         builtin is reachable.
      3. `globals` and `locals` are passed as empty dicts so no name
         binding is reachable.

    If the regex ever needs to be loosened, prefer `ast.parse(..., mode='eval')`
    + a node-type allowlist over relaxing these guards.

    Returns the result formatted as a string so it can be inlined
    verbatim into the agent's reasoning trace.
    """
    expr = (expression or "").strip()
    if not expr:
        return ""
    if not _CALC_ALLOWED.match(expr):
        raise ValueError(f"calculator: unsupported characters in {expr!r}")
    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — namespaced & regex-gated, see SECURITY note
    except Exception as exc:
        raise ValueError(f"calculator: failed to evaluate {expr!r}: {exc}") from exc
    if isinstance(value, float):
        # Trim noisy trailing zeros for nicer agent prompts.
        return (f"{value:.10f}".rstrip("0").rstrip("."))
    return str(value)


# ---------------------------------------------------------------------------
# web_search (stub — production integration roadmap in the TODO below)
# ---------------------------------------------------------------------------


# TODO(T3.2+): wire `run_web_search` to real data sources.
#   Today this returns a deterministic fake so the agent graph can be
#   exercised end-to-end without network. The eventual integration
#   plan:
#     1. company_db_lookup(query, workspace_id) — connects to the
#        internal Postgres read replica + permissions filter. Will
#        reuse `acl_filter.build_user_filter`.
#     2. external_search(query) — wraps the corporate search vendor
#        (Bing/Elastic/etc) behind a feature flag.
#     3. knowledge_graph_query(entity) — joins against an internal KG
#        once it lands.
#   Each of the three will be its own module under `agent_core/tools/`
#   with its own ACL story and audit row. Until then `run_web_search`
#   returns the placeholder below and is intentionally lossy so any
#   accidental production use is obvious in logs.
def run_web_search(query: str, *, max_results: int = 3) -> list[dict[str, Any]]:
    """Stub web/company search — returns a single deterministic placeholder.

    The placeholder includes the query so the agent has *some* signal
    to incorporate into its synthesis, but every caller (and audit
    reviewer) can see at a glance that this is a stub.
    """
    snippet = (
        f"[stub-search] no real results wired yet for query={query!r}. "
        "Will be replaced by company_db / external_search / KG lookups."
    )
    return [
        {
            "title": "Stub result",
            "url": "",
            "snippet": snippet,
            "source": "stub",
        }
    ][: max(0, max_results)]


# ---------------------------------------------------------------------------
# Cached wrapper for graph use
# ---------------------------------------------------------------------------


class CachedRetrievalTool:
    """Memoize the most recent retrieval so `collect_fallback_signal`
    doesn't double the Qdrant round-trips during a graph step.

    The agent graph creates one of these per run and passes the same
    instance to both the retrieve node and the reflect node.
    """

    def __init__(self, searcher: RetrievalFn) -> None:
        self._searcher = searcher
        self.last_result: Any | None = None
        self.last_query: str = ""

    def run(
        self, sub_query: str, *, acl_filter: Any | None = None
    ) -> list[SearchResult]:
        """Run one retrieval, dedupe by chunk_id, cache the result.

        The dedupe runs over `self.last_result` (the single Qdrant
        round-trip we just made) — we deliberately do *not* re-invoke
        the searcher inside `run_retrieval`, otherwise tests using a
        queue-backed fake would see the second call return empty.
        """
        self.last_query = sub_query
        self.last_result = self._searcher.two_stage_search(
            sub_query, acl_filter=acl_filter
        )
        seen: set[str] = set()
        out: list[SearchResult] = []
        for hit in list(getattr(self.last_result, "parents", [])) + list(
            getattr(self.last_result, "children", [])
        ):
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            out.append(hit)
        return out

    @property
    def fallback_triggered(self) -> bool:
        if self.last_result is None:
            return False
        return bool(getattr(self.last_result, "fallback_triggered", False))


# ---------------------------------------------------------------------------
# Result → state helpers (used by graph nodes)
# ---------------------------------------------------------------------------


def search_results_to_state_payload(
    hits: list[SearchResult],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Serialize a list of SearchResults for inclusion in `AgentState`.

    Returns `(chunk_dicts, chunk_ids)`. `chunk_dicts` is suitable for
    JSON serialization (langgraph state must be JSON-friendly);
    `chunk_ids` is a dedup-preserving-order list used by the citation
    writer.
    """
    chunk_dicts: list[dict[str, Any]] = []
    chunk_ids: list[str] = []
    for hit in hits:
        chunk_dicts.append(asdict(hit))
        if hit.chunk_id not in chunk_ids:
            chunk_ids.append(hit.chunk_id)
    return chunk_dicts, chunk_ids