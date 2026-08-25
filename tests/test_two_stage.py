"""Tests for `TwoStageSearcher` (T2.3).

Covers:
  * Constructor validates parent_top_k / child_top_k / fallback_threshold
  * Stage 1 → Stage 2 flow with fake searcher returning predetermined lists
  * `fallback_triggered` toggles based on top-1 parent score
  * Empty stage-1 result → empty stage-2 + fallback off
  * ACL filter is AND-combined with the stage filter (preserves `must`)
  * Empty / whitespace query raises TwoStageError
  * Stage-2 filter restricts to the parent ids returned by stage 1
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client.http import models as qmodels

from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.two_stage import (
    PAYLOAD_IS_PARENT,
    PAYLOAD_PARENT_CHUNK_ID,
    TwoStageError,
    TwoStageResult,
    TwoStageSearcher,
)


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


def _make_result(
    chunk_id: str,
    score: float,
    *,
    is_parent: bool = False,
    parent_id: str | None = None,
    content: str = "",
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=content or f"content for {chunk_id}",
        score=score,
        payload={
            "chunk_id": chunk_id,
            "is_parent": is_parent,
            "parent_chunk_id": parent_id,
            "content": content or f"content for {chunk_id}",
        },
    )


class FakeSearcher:
    """Programmable fake — each call gets a fresh SearchResult list."""

    def __init__(self, queue: list[list[SearchResult]] | None = None) -> None:
        self._queue = list(queue or [])
        self.calls: list[dict[str, Any]] = []
        # When the queue runs dry, return [] rather than blowing up.
        self.default_return: list[SearchResult] = []

    def hybrid_search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float | None = None,
        qdrant_filter: qmodels.Filter | None = None,
    ) -> list[SearchResult]:
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "score_threshold": score_threshold,
                "qdrant_filter": qdrant_filter,
            }
        )
        if self._queue:
            return self._queue.pop(0)
        return list(self.default_return)


# ---------------------------------------------------------------------------
# constructor
# ---------------------------------------------------------------------------


def test_constructor_rejects_invalid_top_k() -> None:
    fake = FakeSearcher()
    with pytest.raises(TwoStageError):
        TwoStageSearcher(fake, parent_top_k=0)
    with pytest.raises(TwoStageError):
        TwoStageSearcher(fake, child_top_k=0)


def test_constructor_rejects_invalid_threshold() -> None:
    fake = FakeSearcher()
    with pytest.raises(TwoStageError):
        TwoStageSearcher(fake, fallback_threshold=-0.1)
    with pytest.raises(TwoStageError):
        TwoStageSearcher(fake, fallback_threshold=1.5)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_two_stage_returns_parents_and_children() -> None:
    parents = [
        _make_result("p1", 0.9, is_parent=True),
        _make_result("p2", 0.7, is_parent=True),
        _make_result("p3", 0.5, is_parent=True),
    ]
    children = [
        _make_result("c1", 0.8, is_parent=False, parent_id="p1"),
        _make_result("c2", 0.6, is_parent=False, parent_id="p1"),
    ]
    fake = FakeSearcher(queue=[parents, children])
    s = TwoStageSearcher(fake, parent_top_k=3, child_top_k=10, fallback_threshold=0.5)

    result = s.two_stage_search("anything")

    assert isinstance(result, TwoStageResult)
    assert [r.chunk_id for r in result.parents] == ["p1", "p2", "p3"]
    assert [r.chunk_id for r in result.children] == ["c1", "c2"]
    assert result.top_score == pytest.approx(0.9)
    assert result.fallback_triggered is False


def test_two_stage_calls_hybrid_search_twice() -> None:
    fake = FakeSearcher(
        queue=[[_make_result("p1", 0.9, is_parent=True)], []]
    )
    s = TwoStageSearcher(fake, parent_top_k=3, child_top_k=10)
    s.two_stage_search("hello")
    assert len(fake.calls) == 2
    assert fake.calls[0]["top_k"] == 3
    assert fake.calls[1]["top_k"] == 10


def test_two_stage_passes_same_query_to_both_stages() -> None:
    fake = FakeSearcher(
        queue=[[_make_result("p1", 0.9, is_parent=True)], []]
    )
    s = TwoStageSearcher(fake)
    s.two_stage_search("specific topic")
    assert fake.calls[0]["query"] == "specific topic"
    assert fake.calls[1]["query"] == "specific topic"


# ---------------------------------------------------------------------------
# stage-2 filter targets parent ids from stage 1
# ---------------------------------------------------------------------------


def test_stage2_filter_includes_parent_ids_from_stage1() -> None:
    parents = [
        _make_result("p1", 0.9, is_parent=True),
        _make_result("p2", 0.7, is_parent=True),
        _make_result("p3", 0.5, is_parent=True),
    ]
    fake = FakeSearcher(queue=[parents, []])
    s = TwoStageSearcher(fake)
    s.two_stage_search("q")
    stage2_filter = fake.calls[1]["qdrant_filter"]
    assert stage2_filter is not None
    # The filter must reference the parent ids returned by stage 1.
    must_clauses = list(stage2_filter.must or [])
    found = False
    for clause in must_clauses:
        if getattr(clause, "key", None) == PAYLOAD_PARENT_CHUNK_ID:
            match = clause.match
            values = getattr(match, "any", None) or []
            assert sorted(values) == ["p1", "p2", "p3"]
            found = True
    assert found, f"parent_id filter not found: {must_clauses}"


def test_stage1_filter_restricts_to_is_parent() -> None:
    fake = FakeSearcher(
        queue=[[_make_result("p1", 0.9, is_parent=True)], []]
    )
    s = TwoStageSearcher(fake)
    s.two_stage_search("q")
    stage1_filter = fake.calls[0]["qdrant_filter"]
    assert stage1_filter is not None
    must_clauses = list(stage1_filter.must or [])
    keys = {getattr(c, "key", None) for c in must_clauses}
    assert PAYLOAD_IS_PARENT in keys


# ---------------------------------------------------------------------------
# fallback signal
# ---------------------------------------------------------------------------


def test_fallback_triggered_when_top1_score_below_threshold() -> None:
    parents = [
        _make_result("p1", 0.3, is_parent=True),  # below threshold
        _make_result("p2", 0.2, is_parent=True),
    ]
    fake = FakeSearcher(queue=[parents, []])
    s = TwoStageSearcher(fake, fallback_threshold=0.5)
    result = s.two_stage_search("weak match")
    assert result.fallback_triggered is True
    assert result.top_score == pytest.approx(0.3)


def test_fallback_not_triggered_when_top1_at_threshold() -> None:
    parents = [
        _make_result("p1", 0.5, is_parent=True),  # equal — not strictly less
    ]
    fake = FakeSearcher(queue=[parents, []])
    s = TwoStageSearcher(fake, fallback_threshold=0.5)
    result = s.two_stage_search("edge")
    assert result.fallback_triggered is False


def test_fallback_not_triggered_when_top1_above_threshold() -> None:
    parents = [_make_result("p1", 0.9, is_parent=True)]
    fake = FakeSearcher(queue=[parents, []])
    s = TwoStageSearcher(fake, fallback_threshold=0.5)
    result = s.two_stage_search("strong")
    assert result.fallback_triggered is False


def test_no_parents_no_fallback_no_children() -> None:
    fake = FakeSearcher(queue=[[], []])
    s = TwoStageSearcher(fake)
    result = s.two_stage_search("nothing matches")
    assert result.parents == []
    assert result.children == []
    assert result.fallback_triggered is False
    assert result.top_score == 0.0
    # Stage 2 must NOT be called when stage 1 returned nothing.
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_empty_query_raises() -> None:
    fake = FakeSearcher()
    s = TwoStageSearcher(fake)
    with pytest.raises(TwoStageError):
        s.two_stage_search("")
    with pytest.raises(TwoStageError):
        s.two_stage_search("   ")


# ---------------------------------------------------------------------------
# ACL filter combination
# ---------------------------------------------------------------------------


def test_acl_filter_is_merged_into_stage_filters() -> None:
    parents = [_make_result("p1", 0.9, is_parent=True)]
    fake = FakeSearcher(queue=[parents, []])
    s = TwoStageSearcher(fake)
    acl = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="workspace_id", match=qmodels.MatchValue(value="ws-1")
            )
        ]
    )
    s.two_stage_search("q", acl_filter=acl)
    for call in fake.calls:
        merged = call["qdrant_filter"]
        assert merged is not None
        keys = {getattr(c, "key", None) for c in (merged.must or [])}
        # The ACL workspace_id clause AND the stage-specific clause both
        # appear in the merged `must` list.
        assert "workspace_id" in keys
        assert PAYLOAD_IS_PARENT in keys or PAYLOAD_PARENT_CHUNK_ID in keys
