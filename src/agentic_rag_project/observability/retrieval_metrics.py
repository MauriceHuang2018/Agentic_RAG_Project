"""Retrieval-path L1 metrics (T4.3).

Called from `retrieval_direct.search.hybrid_search` (or the
two-stage wrapper) to record per-call latency and the embedding
cache hit rate. Kept here so chat / agent code doesn't import
the search layer just to bump a counter.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from agentic_rag_project.observability.registry import get_metrics


def inc_embedding_cache_hit(*, cache_type: str) -> None:
    """`cache_type` ∈ {"dense", "sparse"}."""
    get_metrics().embedding_cache_hits_total.labels(
        cache_type=cache_type
    ).inc()


@contextmanager
def retrieval_latency_timer(
    *, retriever: str, top_k: int
) -> Iterator[None]:
    """Record latency for one retrieval call."""
    metrics = get_metrics()
    start = __import__("time").perf_counter()
    try:
        yield
    finally:
        elapsed = __import__("time").perf_counter() - start
        metrics.retrieval_latency_seconds.labels(
            retriever=retriever, top_k=str(top_k)
        ).observe(elapsed)
        metrics.retrieval_requests_total.labels(
            retriever=retriever, top_k=str(top_k)
        ).inc()


__all__ = [
    "inc_embedding_cache_hit",
    "retrieval_latency_timer",
]
