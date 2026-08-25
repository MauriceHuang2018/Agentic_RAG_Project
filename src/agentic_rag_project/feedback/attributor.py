"""AutoAttributor — rule-based cascade for feedback attribution (T4.2).

Implements the 5-step cascade from the user's pseudocode:

    1. is_ambiguous(query) | is_out_of_scope(query, kb_topics)
            → user_query
    2. context_relevance < 0.5 | recall == 0
            → retrieval
    3. has_boundary_cutoff(answer, retrieved_chunks)
            → chunking
    4. faithfulness < 0.6 & context_relevance > 0.7
            → generation
    5. has_document_conflict(retrieved_chunks) | is_document_expired(...)
            → knowledge
    6. default
            → generation

Why deterministic (no LLM)?
  * Auditable: ops can replay a feedback row through the rules
    and reproduce the verdict.
  * Cheap: feedback pipelines run for every dislike, so a 5 ms
    rule beats a 500 ms LLM call.
  * Predictable: the user's pseudocode is explicit about which
    RAGAS scores trigger which bucket.

The `AutoAttributor` is a stateless service: callers pass in the
inputs and a category-key lookup (default: built-in presets); the
output is an `AttributionResult` carrying the matched rule label
so the dashboard can explain "why" each row was attributed the
way it was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from agentic_rag_project.feedback.categories import DEFAULT_CATEGORIES, find_by_key
from agentic_rag_project.feedback.conflict import (
    has_document_conflict,
    is_document_expired,
)
from agentic_rag_project.feedback.cutoff import has_boundary_cutoff
from agentic_rag_project.feedback.scope import (
    is_ambiguous,
    is_out_of_scope,
)

logger = logging.getLogger(__name__)


# RAGAS score names we read from `feedback.ragas_scores`.
_RAGAS_CONTEXT_RELEVANCE = "context_relevance"  # ≈ context_precision
_RAGAS_RECALL = "recall"                        # ≈ context_recall
_RAGAS_FAITHFULNESS = "faithfulness"

# Thresholds — picked from the user's pseudocode verbatim.
_CONTEXT_RELEVANCE_LOW = 0.5
_FAITHFULNESS_LOW = 0.6
_CONTEXT_RELEVANCE_HIGH = 0.7


@dataclass(frozen=True)
class AttributionResult:
    """Output of `AutoAttributor.attribute()`."""

    category_key: str
    matched_rule: str
    confidence: float
    reasoning: str = ""
    debug: dict[str, Any] = field(default_factory=dict)


class AttributionError(Exception):
    """Wraps any unexpected error during attribution."""


class AutoAttributor:
    """Rule-based cascade attributor."""

    def __init__(
        self,
        *,
        kb_topics: list[str] | None = None,
        category_keys: tuple[str, ...] | None = None,
        max_year_age: int = 2,
    ) -> None:
        # Whitelist of acceptable category keys; used to validate the
        # output of the cascade so a typo can't slip through.
        self._category_keys: tuple[str, ...] = (
            category_keys
            or tuple(spec.key for spec in DEFAULT_CATEGORIES)
        )
        self._kb_topics = list(kb_topics or [])
        self._max_year_age = max_year_age

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def attribute(
        self,
        *,
        query: str,
        answer: str,
        retrieved_chunks: list[str],
        ragas_scores: Mapping[str, Any] | None = None,
        reference_year: int | None = None,
    ) -> AttributionResult:
        """Run the cascade; return the first matching category.

        `retrieved_chunks` should be a list of plain strings — the
        chunk `content` (or any surrogate). Pass empty list when
        retrieval returned nothing; the cascade will still run, but
        the `recall==0` short-circuit at step 2 fires first.
        """
        scores: dict[str, Any] = dict(ragas_scores or {})
        context_relevance = _safe_float(scores.get(_RAGAS_CONTEXT_RELEVANCE))
        recall = _safe_float(scores.get(_RAGAS_RECALL))
        faithfulness = _safe_float(scores.get(_RAGAS_FAITHFULNESS))

        # Step 1 — user query.
        ambiguity = is_ambiguous(query)
        if ambiguity.matched:
            return self._make_result(
                "user_query",
                matched_rule="user_query:ambiguous",
                reason=ambiguity.reason,
                debug={"ambiguity": ambiguity.reason},
            )
        scope = is_out_of_scope(query, self._kb_topics)
        if scope.matched:
            return self._make_result(
                "user_query",
                matched_rule="user_query:out_of_scope",
                reason=scope.reason,
                debug={"scope": scope.reason},
            )

        # Step 2 — retrieval.
        if (
            (context_relevance is not None and context_relevance < _CONTEXT_RELEVANCE_LOW)
            or (recall is not None and recall == 0)
        ):
            return self._make_result(
                "retrieval",
                matched_rule="retrieval:low_context_relevance_or_zero_recall",
                reason="context_relevance<0.5 or recall==0",
                debug={
                    "context_relevance": context_relevance,
                    "recall": recall,
                },
            )

        # Step 3 — chunking.
        cutoff = has_boundary_cutoff(answer, retrieved_chunks)
        if cutoff.matched:
            return self._make_result(
                "chunking",
                matched_rule="chunking:boundary_cutoff",
                reason=cutoff.reason,
                debug={"cutoff": cutoff.reason},
            )

        # Step 4 — generation.
        if (
            faithfulness is not None
            and context_relevance is not None
            and faithfulness < _FAITHFULNESS_LOW
            and context_relevance > _CONTEXT_RELEVANCE_HIGH
        ):
            return self._make_result(
                "generation",
                matched_rule="generation:low_faithfulness_high_relevance",
                reason="faithfulness<0.6 and context_relevance>0.7",
                debug={
                    "faithfulness": faithfulness,
                    "context_relevance": context_relevance,
                },
            )

        # Step 5 — knowledge.
        conflict = has_document_conflict(retrieved_chunks)
        if conflict.matched:
            return self._make_result(
                "knowledge",
                matched_rule="knowledge:document_conflict",
                reason=conflict.reason,
                debug={"conflict": conflict.reason},
            )
        expired = is_document_expired(
            retrieved_chunks,
            max_year_age=self._max_year_age,
            reference_year=reference_year,
        )
        if expired.matched:
            return self._make_result(
                "knowledge",
                matched_rule="knowledge:document_expired",
                reason=expired.reason,
                debug={"expired": expired.reason},
            )

        # Step 6 — default.
        return self._make_result(
            "generation",
            matched_rule="generation:default",
            reason="no other rule matched; defaulting to generation",
            debug={},
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_result(
        self,
        category_key: str,
        *,
        matched_rule: str,
        reason: str,
        debug: dict[str, Any],
    ) -> AttributionResult:
        if category_key not in self._category_keys:
            # Don't accept arbitrary strings — protects against typos
            # in the cascade itself.
            raise AttributionError(
                f"attributor produced unknown category {category_key!r}; "
                f"allowed: {self._category_keys}"
            )
        # Built-in categories get a confidence of 1.0; admin-added
        # categories don't go through this path because the cascade
        # only emits built-in keys.
        confidence = 1.0
        return AttributionResult(
            category_key=category_key,
            matched_rule=matched_rule,
            confidence=confidence,
            reasoning=reason,
            debug=debug,
        )


def _safe_float(value: Any) -> float | None:
    """Coerce a score to float; None / unparseable → None."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bools are ints in Python — reject explicitly.
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "AttributionError",
    "AttributionResult",
    "AutoAttributor",
]
