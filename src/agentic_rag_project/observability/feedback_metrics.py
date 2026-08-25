"""Feedback-path L1 metrics (T4.3).

Called from `FeedbackService.submit()` after each submission to
emit the per-rating / per-category counter. The 7-day-window
business gauges (csat, dislike_rate) are derived by the L3
collector — those reads are too expensive for the hot path.
"""

from __future__ import annotations

from agentic_rag_project.feedback.models import (
    AttributionStatus,
    FeedbackRating,
)
from agentic_rag_project.observability.registry import get_metrics


def record_feedback(
    *,
    rating: FeedbackRating | str,
    attribution_status: AttributionStatus | str,
    category_key: str | None,
) -> None:
    """Increment `feedback_total{...}`.

    `rating` and `attribution_status` accept either the enum or
    the raw `.value` string so callers don't have to import the
    enum types.
    """
    rating_key = rating.value if hasattr(rating, "value") else str(rating)
    status_key = (
        attribution_status.value
        if hasattr(attribution_status, "value")
        else str(attribution_status)
    )
    cat_key = category_key or "none"
    get_metrics().feedback_total.labels(
        rating=rating_key,
        attribution_status=status_key,
        category_key=cat_key,
    ).inc()


__all__ = ["record_feedback"]
