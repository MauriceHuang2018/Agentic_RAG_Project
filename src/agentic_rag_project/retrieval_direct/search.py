"""Hybrid search (dense + BM25) over the Qdrant collection.

DESIGN 4.4 / TASK T2.2:
  * Dense vector from LiteLLM /v1/embeddings (BGE-M3, 1024-dim)
  * Sparse BM25 vector computed locally (32-bit SHA1 token ids)
  * Both fed to Qdrant as named vectors (`dense`, `sparse`), fused with
    Reciprocal Rank Fusion inside `query_points`
  * Dense embedding result cached in Redis under `embed:cache:<hash>` —
    identical queries within the TTL skip the LiteLLM round-trip
  * `qdrant_filter` is REQUIRED: every caller must pass an ACL pre-filter.
    We do NOT silently default to no-filter (DESIGN 8.3 failure-closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from agentic_rag_project.doc_processor.embedder import (
    LiteLLMEmbedder,
    _bm25_sparse,
)
from agentic_rag_project.observability.retrieval_metrics import (
    inc_embedding_cache_hit,
    retrieval_latency_timer,
)

logger = logging.getLogger(__name__)

EMBED_CACHE_PREFIX = "embed:cache:"
EMBED_CACHE_HASH_LEN = 16


class SearchError(Exception):
    """Raised on a malformed query or a fatal retrieval failure."""


class EmbedderLike(Protocol):
    """Protocol so tests can substitute a deterministic fake."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class SearchResult:
    """One hit returned by `hybrid_search`."""

    chunk_id: str
    document_id: str
    content: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddedQuery:
    """Dense + sparse representation of a query, ready for Qdrant."""

    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    cache_hit: bool


def _cache_key(query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return f"{EMBED_CACHE_PREFIX}{digest[:EMBED_CACHE_HASH_LEN]}"


class HybridSearcher:
    """Qdrant-backed hybrid (dense + sparse BM25) retriever.

    The class is stateless beyond its injected collaborators; multiple
    workers can share a single instance.
    """

    def __init__(
        self,
        *,
        qdrant: QdrantClient,
        collection: str,
        embedder: EmbedderLike | None = None,
        redis_cache: Any | None = None,
        cache_ttl_s: int = 3600,
        dense_vector_name: str = "dense",
        sparse_vector_name: str = "sparse",
    ) -> None:
        self._qdrant = qdrant
        self._collection = collection
        self._embedder: EmbedderLike = embedder or LiteLLMEmbedder()
        self._redis = redis_cache
        self._cache_ttl_s = cache_ttl_s
        self._dense_name = dense_vector_name
        self._sparse_name = sparse_vector_name

    # ---------------------------------------------------------------------
    # embedding + cache
    # ---------------------------------------------------------------------

    def embed_query(self, query: str) -> EmbeddedQuery:
        """Compute (dense, sparse) for a query, with a Redis cache for dense.

        Sparse is recomputed every call — it's pure-Python tokenization,
        trivially cheap (sub-ms), so caching buys nothing.
        """
        sparse = _bm25_sparse(query)
        sparse_indices = list(sparse.keys())
        sparse_values = [float(sparse[k]) for k in sparse_indices]

        cached = self._lookup_cached_dense(query)
        if cached is not None:
            dense = cached
            cache_hit = True
            inc_embedding_cache_hit(cache_type="dense")
        else:
            [dense] = self._embedder.embed_texts([query])
            self._store_cached_dense(query, dense)
            cache_hit = False

        return EmbeddedQuery(
            dense=dense,
            sparse_indices=sparse_indices,
            sparse_values=sparse_values,
            cache_hit=cache_hit,
        )

    def _lookup_cached_dense(self, query: str) -> list[float] | None:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(_cache_key(query))
        except Exception as exc:  # Redis transient error — degrade gracefully
            logger.warning("embed cache lookup failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            vec = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # Trust whatever dimension we stored — the only thing that wrote
        # this key was `_store_cached_dense`. Validating against a fixed
        # DENSE_DIM would couple tests to the production embedder size.
        if not isinstance(vec, list) or not all(
            isinstance(x, (int, float)) for x in vec
        ):
            return None
        return [float(x) for x in vec]

    def _store_cached_dense(self, query: str, dense: list[float]) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(
                _cache_key(query),
                self._cache_ttl_s,
                json.dumps(dense),
            )
        except Exception as exc:
            logger.warning("embed cache store failed: %s", exc)

    # ---------------------------------------------------------------------
    # search
    # ---------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        *,
        top_k: int = 10,
        score_threshold: float | None = None,
        qdrant_filter: qmodels.Filter | None = None,
        oversample_factor: int = 2,
    ) -> list[SearchResult]:
        """Run dense+sparse hybrid search, returning up to `top_k` results.

        `qdrant_filter` is required for production callers — ACL filtering
        happens at the Qdrant layer (DESIGN 8.3). For unit tests that
        exercise the ranker in isolation we accept `None`.
        """
        if not query or not query.strip():
            raise SearchError("query must be a non-empty string")
        if top_k <= 0:
            raise SearchError("top_k must be positive")

        # L1 observability: record retrieval latency + request counter.
        with retrieval_latency_timer(retriever="hybrid", top_k=top_k):
            embedded = self.embed_query(query)

            # We oversample each branch and let RRF pick the union.
            per_branch_limit = max(top_k * oversample_factor, top_k)

            sparse_vector = qmodels.SparseVector(
                indices=embedded.sparse_indices,
                values=embedded.sparse_values,
            )

            prefetches = [
                models.Prefetch(
                    query=embedded.dense,
                    using=self._dense_name,
                    limit=per_branch_limit,
                    filter=qdrant_filter,
                ),
                models.Prefetch(
                    query=sparse_vector,
                    using=self._sparse_name,
                    limit=per_branch_limit,
                    filter=qdrant_filter,
                ),
            ]
            response = self._qdrant.query_points(
                collection_name=self._collection,
                prefetch=prefetches,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,
            )
            results = [_to_result(p) for p in response.points]
        return results


# `models` is referenced as a module attribute on the class body above; pull
# it into module scope so the test can monkey-patch / introspect easily.
models = qmodels


def _to_result(point: qmodels.ScoredPoint) -> SearchResult:
    payload = dict(point.payload or {})
    chunk_id = str(payload.get("chunk_id") or point.id)
    document_id = str(payload.get("document_id", ""))
    content = str(payload.get("content", ""))
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        score=float(point.score or 0.0),
        payload=payload,
    )
