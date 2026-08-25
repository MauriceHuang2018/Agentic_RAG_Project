"""Two-stage retrieval (parent → child) over the Qdrant collection.

DESIGN 4.4 / TASK T2.3:

  Stage 1: hybrid_search filtered to `is_parent=true`, take top-N parents.
  Stage 2: hybrid_search filtered to `parent_chunk_id IN [top-N parent ids]`,
           take top-M children.
  Fallback: if the top-1 parent's score is below `fallback_threshold`, set
           `fallback_triggered=True` so the downstream 128K fallback (T3.3)
           knows to switch models.

The class wraps a `HybridSearcher` instance — T2.2 owns the embedder,
Redis cache, and Qdrant call; T2.3 owns only the orchestration + filter
construction. This split keeps T2.3 trivially testable with a fake
searcher that just returns predetermined `SearchResult` lists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from qdrant_client.http import models as qmodels

from agentic_rag_project.retrieval_direct.search import SearchResult

logger = logging.getLogger(__name__)

# Qdrant payload keys (mirror DESIGN 4.3 + indexer payload).
PAYLOAD_IS_PARENT = "is_parent"
PAYLOAD_PARENT_CHUNK_ID = "parent_chunk_id"
PAYLOAD_CHUNK_ID = "chunk_id"


class SearcherLike(Protocol):
    """Protocol — anything with a `hybrid_search(query, *, top_k, qdrant_filter)` works."""

    def hybrid_search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float | None = None,
        qdrant_filter: qmodels.Filter | None = None,
    ) -> list[SearchResult]: ...


@dataclass(frozen=True)
class TwoStageResult:
    """Outcome of `TwoStageSearcher.two_stage_search`."""

    parents: list[SearchResult] = field(default_factory=list)
    children: list[SearchResult] = field(default_factory=list)
    fallback_triggered: bool = False
    top_score: float = 0.0
    stage1_query: str = ""
    stage2_query: str = ""


class TwoStageError(Exception):
    """Raised on configuration or query errors."""


def _is_parent_filter() -> qmodels.Filter:
    """Payload filter restricting Qdrant to parent chunks."""
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key=PAYLOAD_IS_PARENT, match=qmodels.MatchValue(value=True)
            )
        ]
    )


def _parent_id_filter(parent_ids: list[str]) -> qmodels.Filter:
    """Payload filter restricting Qdrant to children of the given parents."""
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key=PAYLOAD_PARENT_CHUNK_ID,
                match=qmodels.MatchAny(any=parent_ids),
            )
        ]
    )


class TwoStageSearcher:
    """Orchestrate the two-stage retrieval strategy.

    Both stages reuse the wrapped `HybridSearcher`; we only differ by the
    Qdrant filter passed to each call.
    """

    def __init__(
        self,
        searcher: SearcherLike,
        *,
        parent_top_k: int = 3,
        child_top_k: int = 10,
        fallback_threshold: float = 0.5,
    ) -> None:
        if parent_top_k <= 0:
            raise TwoStageError("parent_top_k must be positive")
        if child_top_k <= 0:
            raise TwoStageError("child_top_k must be positive")
        if not 0.0 <= fallback_threshold <= 1.0:
            raise TwoStageError(
                "fallback_threshold must lie in [0, 1] (RRF-style scores)"
            )
        self._searcher = searcher
        self._parent_top_k = parent_top_k
        self._child_top_k = child_top_k
        self._fallback_threshold = fallback_threshold

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    @property
    def parent_top_k(self) -> int:
        return self._parent_top_k

    @property
    def child_top_k(self) -> int:
        return self._child_top_k

    def two_stage_search(
        self,
        query: str,
        *,
        acl_filter: qmodels.Filter | None = None,
    ) -> TwoStageResult:
        """Run stage 1 (parents) and stage 2 (children) and decide on fallback.

        `acl_filter` is AND-combined with the stage-specific payload filter
        so ACL pre-filtering (T5.3) keeps working unchanged.
        """
        if not query or not query.strip():
            raise TwoStageError("query must be a non-empty string")

        # Stage 1 — locate top-N parent chapters.
        stage1_filter = _and(acl_filter, _is_parent_filter())
        parents = self._searcher.hybrid_search(
            query,
            top_k=self._parent_top_k,
            qdrant_filter=stage1_filter,
        )

        if not parents:
            return TwoStageResult(
                parents=[],
                children=[],
                fallback_triggered=False,
                top_score=0.0,
                stage1_query=query,
                stage2_query="",
            )

        top_score = parents[0].score
        fallback_triggered = top_score < self._fallback_threshold

        # Stage 2 — fetch top-M children under the chosen parents.
        parent_ids = [p.chunk_id for p in parents]
        stage2_filter = _and(acl_filter, _parent_id_filter(parent_ids))
        children = self._searcher.hybrid_search(
            query,
            top_k=self._child_top_k,
            qdrant_filter=stage2_filter,
        )

        return TwoStageResult(
            parents=parents,
            children=children,
            fallback_triggered=fallback_triggered,
            top_score=top_score,
            stage1_query=query,
            stage2_query=query,
        )


def _and(
    base: qmodels.Filter | None, extra: qmodels.Filter | None
) -> qmodels.Filter | None:
    """AND-combine two Qdrant filters; tolerate either side being None."""
    if base is None and extra is None:
        return None
    if base is None:
        return extra
    if extra is None:
        return base
    # Merge: keep all `must` clauses from both filters, `should` clauses
    # only from the base filter (which encodes ACL semantics).
    must = list(base.must or []) + list(extra.must or [])
    return qmodels.Filter(
        must=must,
        should=base.should,
        must_not=base.must_not,
    )
