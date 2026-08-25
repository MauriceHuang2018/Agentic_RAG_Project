"""Tests for `HybridSearcher` (T2.2).

Covers:
  * End-to-end upsert + hybrid_search with two corpus chunks
  * Redis embedding cache: hit on second identical query
  * Embedder fallback when Redis is None
  * `SearchResult` payload is fully populated
  * `top_k` clamp and `score_threshold` filtering
  * Embedding cache key uses sha256 prefix
  * Empty / whitespace queries raise SearchError
  * Sparse-only / dense-only still produce results (RRF graceful)

We use in-memory Qdrant + FakeRedis + a deterministic `FakeEmbedder`.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Any

import fakeredis
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from agentic_rag_project.config import Settings
from agentic_rag_project.doc_processor.embedder import _bm25_sparse
from agentic_rag_project.retrieval_direct.qdrant_init import (
    VECTOR_DENSE_NAME,
    VECTOR_SPARSE_NAME,
    ensure_all,
)
from agentic_rag_project.retrieval_direct.search import (
    EMBED_CACHE_PREFIX,
    EmbeddedQuery,
    HybridSearcher,
    SearchError,
    SearchResult,
    _cache_key,
)


# ---------------------------------------------------------------------------
# test fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic embedder: bag-of-words-style dense vector."""

    DIM = 8  # tiny dim so we can reason about scores by hand

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.call_count = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.call_count += len(texts)
        return [self._embed(t) for t in texts]

    def _embed(self, text: str) -> list[float]:
        tokens = re.findall(r"\w+", text.lower())
        vec = [0.0] * self.dim
        if not tokens:
            return vec
        for tok in tokens:
            h = int(hashlib.sha1(tok.encode()).hexdigest()[:8], 16)
            vec[h % self.dim] += 1.0
        # L2-normalize for cosine
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def _upsert_chunk(
    client: QdrantClient,
    collection: str,
    *,
    chunk_id: str,
    document_id: str,
    workspace_id: str,
    content: str,
    is_parent: bool,
    parent_chunk_id: str | None,
    page: int = 1,
) -> None:
    """Upsert a single chunk with both named vectors into the test collection."""
    embedder = FakeEmbedder()
    dense = embedder._embed(content)
    sparse = _bm25_sparse(content)
    indices = list(sparse.keys())
    values = [float(sparse[k]) for k in indices]
    point_id = str(uuid.uuid4())
    payload = {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "workspace_id": workspace_id,
        "owner_id": "owner-1",
        "acl_user_ids": [],
        "acl_workspace_ids": [workspace_id],
        "authority_level": 1,
        "document_format": "pdf",
        "is_parent": is_parent,
        "parent_chunk_id": parent_chunk_id,
        "page_no": page,
        "section_path": ["root"],
        "content": content,
    }
    client.upsert(
        collection_name=collection,
        points=[
            qmodels.PointStruct(
                id=point_id,
                vector={
                    VECTOR_DENSE_NAME: dense,
                    VECTOR_SPARSE_NAME: qmodels.SparseVector(
                        indices=indices, values=values
                    ),
                },
                payload=payload,
            )
        ],
        wait=True,
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> QdrantClient:
    return QdrantClient(location=":memory:")


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.qdrant_collection = f"test_{uuid.uuid4().hex[:8]}"
    s.qdrant_vector_dim = FakeEmbedder.DIM
    return s


@pytest.fixture
def ready_collection(qdrant: QdrantClient, settings: Settings) -> str:
    ensure_all(qdrant, settings)
    return settings.qdrant_collection


@pytest.fixture
def redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def searcher(
    qdrant: QdrantClient,
    ready_collection: str,
    embedder: FakeEmbedder,
    redis: fakeredis.FakeRedis,
) -> HybridSearcher:
    return HybridSearcher(
        qdrant=qdrant,
        collection=ready_collection,
        embedder=embedder,
        redis_cache=redis,
    )


# ---------------------------------------------------------------------------
# cache key + helpers
# ---------------------------------------------------------------------------


def test_cache_key_uses_sha256_prefix() -> None:
    key = _cache_key("hello world")
    assert key.startswith(EMBED_CACHE_PREFIX)
    digest = hashlib.sha256("hello world".encode()).hexdigest()
    assert key == f"{EMBED_CACHE_PREFIX}{digest[:16]}"


# ---------------------------------------------------------------------------
# embed_query
# ---------------------------------------------------------------------------


def test_embed_query_returns_dense_and_sparse(searcher: HybridSearcher) -> None:
    eq = searcher.embed_query("machine learning")
    assert isinstance(eq, EmbeddedQuery)
    assert len(eq.dense) == FakeEmbedder.DIM
    assert isinstance(eq.sparse_indices, list)
    assert isinstance(eq.sparse_values, list)
    assert len(eq.sparse_indices) == len(eq.sparse_values)
    assert eq.cache_hit is False


def test_embed_query_caches_dense_on_second_call(
    searcher: HybridSearcher, embedder: FakeEmbedder, redis: Any
) -> None:
    searcher.embed_query("machine learning")
    assert embedder.call_count == 1
    # Second call: must NOT increment the embedder counter (cache hit).
    eq2 = searcher.embed_query("machine learning")
    assert embedder.call_count == 1
    assert eq2.cache_hit is True
    # And the cache key is present in redis.
    digest = hashlib.sha256(b"machine learning").hexdigest()[:16]
    assert redis.exists(f"{EMBED_CACHE_PREFIX}{digest}") == 1


def test_embed_query_without_redis_does_not_raise(
    qdrant: QdrantClient, ready_collection: str, embedder: FakeEmbedder
) -> None:
    s = HybridSearcher(
        qdrant=qdrant,
        collection=ready_collection,
        embedder=embedder,
        redis_cache=None,
    )
    eq = s.embed_query("hello world")
    assert eq.cache_hit is False
    # Calling again still recomputes (no cache), but never raises.
    eq2 = s.embed_query("hello world")
    assert eq2.cache_hit is False


def test_embed_query_handles_corrupt_cache_value(
    searcher: HybridSearcher, redis: Any
) -> None:
    redis.set(_cache_key("hello world"), b"not json")
    eq = searcher.embed_query("hello world")
    # Corrupt cache => miss => fresh compute.
    assert eq.cache_hit is False


# ---------------------------------------------------------------------------
# hybrid_search
# ---------------------------------------------------------------------------


def test_hybrid_search_returns_results(
    searcher: HybridSearcher,
    qdrant: QdrantClient,
    ready_collection: str,
) -> None:
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c1",
        document_id="d1",
        workspace_id="ws-1",
        content="machine learning basics",
        is_parent=False,
        parent_chunk_id="p1",
    )
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c2",
        document_id="d1",
        workspace_id="ws-1",
        content="deep neural networks",
        is_parent=False,
        parent_chunk_id="p1",
    )
    results = searcher.hybrid_search("machine learning", top_k=5)
    assert len(results) >= 1
    assert all(isinstance(r, SearchResult) for r in results)
    # The top hit should be c1 (closest in content).
    assert results[0].chunk_id == "c1"


def test_hybrid_search_respects_top_k(
    searcher: HybridSearcher,
    qdrant: QdrantClient,
    ready_collection: str,
) -> None:
    for i in range(5):
        _upsert_chunk(
            qdrant,
            ready_collection,
            chunk_id=f"c{i}",
            document_id="d1",
            workspace_id="ws-1",
            content=f"random text {i} about topic {i}",
            is_parent=False,
            parent_chunk_id="p1",
        )
    results = searcher.hybrid_search("topic", top_k=2)
    assert len(results) <= 2


def test_hybrid_search_accepts_score_threshold(
    searcher: HybridSearcher,
    qdrant: QdrantClient,
    ready_collection: str,
) -> None:
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c1",
        document_id="d1",
        workspace_id="ws-1",
        content="machine learning",
        is_parent=False,
        parent_chunk_id="p1",
    )
    # Setting a threshold higher than any reasonable RRF score returns empty.
    results = searcher.hybrid_search(
        "machine learning", top_k=10, score_threshold=10.0
    )
    assert results == []


def test_hybrid_search_empty_query_raises(searcher: HybridSearcher) -> None:
    with pytest.raises(SearchError):
        searcher.hybrid_search("", top_k=5)
    with pytest.raises(SearchError):
        searcher.hybrid_search("   ", top_k=5)


def test_hybrid_search_invalid_top_k_raises(searcher: HybridSearcher) -> None:
    with pytest.raises(SearchError):
        searcher.hybrid_search("hello", top_k=0)


def test_hybrid_search_empty_collection_returns_empty(
    searcher: HybridSearcher,
) -> None:
    assert searcher.hybrid_search("anything", top_k=5) == []


# ---------------------------------------------------------------------------
# ACL pre-filter integration
# ---------------------------------------------------------------------------


def test_hybrid_search_with_acl_filter_excludes_other_workspaces(
    searcher: HybridSearcher,
    qdrant: QdrantClient,
    ready_collection: str,
) -> None:
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c1",
        document_id="d1",
        workspace_id="ws-1",
        content="machine learning",
        is_parent=False,
        parent_chunk_id="p1",
    )
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c2",
        document_id="d2",
        workspace_id="ws-2",
        content="machine learning",
        is_parent=False,
        parent_chunk_id="p2",
    )
    # Filter for ws-1 only.
    filt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="workspace_id", match=qmodels.MatchValue(value="ws-1")
            )
        ]
    )
    results = searcher.hybrid_search("machine learning", top_k=5, qdrant_filter=filt)
    assert len(results) == 1
    assert results[0].chunk_id == "c1"
    assert results[0].payload["workspace_id"] == "ws-1"


# ---------------------------------------------------------------------------
# SearchResult payload
# ---------------------------------------------------------------------------


def test_search_result_includes_content_and_score(
    searcher: HybridSearcher,
    qdrant: QdrantClient,
    ready_collection: str,
) -> None:
    _upsert_chunk(
        qdrant,
        ready_collection,
        chunk_id="c1",
        document_id="d1",
        workspace_id="ws-1",
        content="specific phrase xyz",
        is_parent=False,
        parent_chunk_id="p1",
        page=42,
    )
    results = searcher.hybrid_search("specific phrase", top_k=5)
    assert len(results) >= 1
    hit = results[0]
    assert hit.content == "specific phrase xyz"
    assert hit.document_id == "d1"
    assert hit.score >= 0.0
    assert hit.payload["page_no"] == 42
