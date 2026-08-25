"""Evaluation scorer (T4.1).

DESIGN 4.5 / TASK T4.1 — orchestrates per-case metric computation
once a chat query has produced an answer + retrieved chunks. The
scorer is intentionally thin: each metric is a pure function in
`metrics.py`, so this class mostly (a) wires them together, (b)
extracts the citations list and the context texts from the chat
output, and (c) handles the embedder-as-optional case.

Failure mode contract: if every metric raises, we propagate the
exception. If a single metric raises (e.g. the embedder blew up),
we substitute 0.0 with `details.error` set, so the eval report
still has a row for every (case, metric) combination. Aggregate
queries then naturally show the broken metric as a hole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agentic_rag_project.evaluation.metrics import (
    MetricResult,
    compute_all_metrics,
)

logger = logging.getLogger(__name__)


Embedder = Callable[[list[str]], list[list[float]]]
LLMCall = Callable[[str, str, float], str]


@dataclass
class ScoredCase:
    """The scorer's output for one case — pairs each metric with details."""

    case_id: str
    metrics: list[MetricResult] = field(default_factory=list)


class MetricError(Exception):
    """Wraps a single metric's failure so the scorer can continue."""


class EvalScorer:
    """Run all 6 metrics on a (case, chat output) pair."""

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        embedder: Embedder | None = None,
        faithfulness_timeout: float = 30.0,
    ) -> None:
        self._llm_call = llm_call
        self._embedder = embedder
        self._faithfulness_timeout = faithfulness_timeout

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def score(
        self,
        *,
        case_id: str,
        query: str,
        answer: str,
        retrieved_chunks: list[Any],
        contexts: list[str] | None = None,
        expected_answer: str = "",
        expected_chunk_ids: list[str] | None = None,
    ) -> ScoredCase:
        """Score one case.

        `retrieved_chunks` is the list the chat endpoint returned;
        we accept any object with a `.chunk_id` attribute OR a string
        (already an id) OR a dict with key `chunk_id`.

        `contexts` defaults to the chunks' `.content` (or `content`
        dict-key); pass it explicitly if your retrieval store exposes
        richer bodies.
        """
        if contexts is None:
            contexts = self._extract_contents(retrieved_chunks)
        retrieved_ids = self._extract_chunk_ids(retrieved_chunks)
        if expected_chunk_ids is None:
            expected_chunk_ids = []

        metrics = compute_all_metrics(
            query=query,
            answer=answer,
            contexts=contexts,
            retrieved_chunk_ids=retrieved_ids,
            expected_answer=expected_answer,
            expected_chunk_ids=list(expected_chunk_ids),
            llm_call=self._llm_call,
            embedder=self._embedder,
            faithfulness_timeout=self._faithfulness_timeout,
        )
        return ScoredCase(case_id=case_id, metrics=metrics)

    # ------------------------------------------------------------------
    # adapter helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chunk_ids(chunks: list[Any]) -> list[str]:
        ids: list[str] = []
        for c in chunks:
            cid = _chunk_id_of(c)
            if cid:
                ids.append(str(cid))
        return ids

    @staticmethod
    def _extract_contents(chunks: list[Any]) -> list[str]:
        out: list[str] = []
        for c in chunks:
            content = _chunk_content_of(c)
            if content:
                out.append(str(content))
        return out


def _chunk_id_of(obj: Any) -> Any | None:
    """Best-effort extraction of `chunk_id` from SearchResult / dict / str."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("chunk_id") or obj.get("id")
    return getattr(obj, "chunk_id", None) or getattr(obj, "id", None)


def _chunk_content_of(obj: Any) -> Any | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get("content") or obj.get("text")
    return getattr(obj, "content", None) or getattr(obj, "text", None)


__all__ = [
    "Embedder",
    "EvalScorer",
    "LLMCall",
    "MetricError",
    "ScoredCase",
]
