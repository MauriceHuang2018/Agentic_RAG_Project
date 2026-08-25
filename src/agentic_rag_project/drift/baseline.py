"""Baseline computation for the drift_detector (T4.4).

For each `(kind, metric_name, workspace_id)` triple, fetch the
7-day-same-hour-of-day P50. The window is `[now - 7d, now - 1h]`
— excluding the last hour so "baseline" doesn't leak from
"current".

Returns `None` when the sample count is below the cold-start
threshold (default 50). The caller (detector.py) skips on None —
the alternative (returning a fabricated baseline) would generate
false positives during the first week of production.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from collections.abc import Callable
from typing import Final

from sqlalchemy import bindparam, func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Minimum samples before we trust the baseline. Tuned to roughly
# 1 sample per workspace per 3 hours × 7 days = ~56 — but we
# round down to 50 so a slow workspace still gets a baseline.
COLD_START_THRESHOLD: Final[int] = 50

# Hard cap on rows scanned per query. A healthy eval run produces
# ~500 rows/min, so 10k covers ~20 minutes of evaluation history.
_SCAN_LIMIT: Final[int] = 10_000


# ---------------------------------------------------------------------------
# Per-kind query builders. Each takes a session, returns the raw value
# column (so we can wrap with percentile_cont / mean).
# ---------------------------------------------------------------------------


def _eval_quality_value(session: Session, *, workspace_id: uuid.UUID | None):
    """Yield `(timestamp, value)` from evaluation_results for `workspace_id`."""
    from agentic_rag_project.db.models import Conversation, EvaluationResult, Message

    stmt = (
        select(EvaluationResult.ts, EvaluationResult.value)
        .join(Message, Message.id == EvaluationResult.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
    )
    if workspace_id is not None:
        stmt = stmt.where(Conversation.workspace_id == workspace_id)
    return stmt


def _csat_value(session: Session, *, workspace_id: uuid.UUID | None):
    """Per-feedback CSAT rows: 1.0 for LIKE, 0.0 for DISLIKE."""
    from agentic_rag_project.feedback.models import Feedback, FeedbackRating

    value_expr = func.case(
        (Feedback.rating == FeedbackRating.LIKE.value, 1.0),
        (Feedback.rating == FeedbackRating.DISLIKE.value, 0.0),
        else_=None,
    ).label("value")
    stmt = select(Feedback.created_at.label("ts"), value_expr)
    if workspace_id is not None:
        stmt = stmt.where(Feedback.workspace_id == workspace_id)
    return stmt


def _dislike_attribution_value(session: Session, *, workspace_id: uuid.UUID | None):
    """Per-attribution count (1.0 per row). Count aggregated upstream."""
    from agentic_rag_project.feedback.models import (
        Feedback,
        FeedbackAttribution,
        FeedbackCategory,
    )

    stmt = (
        select(Feedback.created_at.label("ts"), FeedbackCategory.key.label("value"))
        .join(FeedbackAttribution, FeedbackAttribution.feedback_id == Feedback.id)
        .join(FeedbackCategory, FeedbackCategory.id == FeedbackAttribution.category_id)
    )
    if workspace_id is not None:
        stmt = stmt.where(Feedback.workspace_id == workspace_id)
    return stmt


# Dispatch — kind → query builder.
_KIND_QUERY: Final[dict[str, Callable]] = {
    "eval_quality": _eval_quality_value,
    "csat": _csat_value,
    "dislike_attribution": _dislike_attribution_value,
}


def _metric_names_for(kind: str) -> tuple[str, ...]:
    """Return the metric names tracked under each kind."""
    if kind == "eval_quality":
        return (
            "faithfulness",
            "citation_accuracy",
            "recall",
            "precision",
            "relevance",
            "hallucination",
        )
    if kind == "csat":
        return ("csat_score",)
    if kind == "dislike_attribution":
        return (
            "retrieval",
            "chunking",
            "generation",
            "knowledge",
            "user_query",
        )
    raise ValueError(f"unknown kind: {kind!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _percentile_query(stmt, metric_name_col, *, cutoff_low, cutoff_high):
    """Wrap `stmt` with a P50 aggregate filtered to [cutoff_low, cutoff_high].

    The metric_name column is supplied by the caller because some
    kinds use `EvaluationResult.metric_name` and others use
    `FeedbackCategory.key`.
    """
    return (
        select(
            metric_name_col.label("metric_name"),
            func.percentile_cont(0.5)
            .within_group(_dt_order_by_value())
            .label("p50"),
            func.count().label("n"),
        )
        .select_from(stmt.subquery())
        .where(stmt.subquery().c.ts >= cutoff_low)
        .where(stmt.subquery().c.ts < cutoff_high)
        .group_by(metric_name_col)
    )


def _dt_order_by_value():
    """Stand-in for SQLAlchemy's order_by clause used by percentile_cont.

    Imported lazily so the module is importable in environments where
    the dialect doesn't support ordered-set aggregates (we only ever
    ship PostgreSQL, but be defensive).
    """
    from sqlalchemy import literal_column

    return literal_column("value")


def _rows_in_window(
    session: Session,
    *,
    kind: str,
    workspace_id: uuid.UUID | None,
    cutoff_low: _dt.datetime,
    cutoff_high: _dt.datetime,
):
    """Yield (metric_name, value) rows in [cutoff_low, cutoff_high] for `kind`.

    Returns a list (not a generator) so we can iterate twice — once
    for the count (cold-start check), once for the P50.
    """
    from agentic_rag_project.db.models import Conversation, EvaluationResult, Message
    from agentic_rag_project.feedback.models import (
        Feedback,
        FeedbackAttribution,
        FeedbackCategory,
    )

    if kind == "eval_quality":
        stmt = (
            select(EvaluationResult.metric_name, EvaluationResult.value)
            .join(Message, Message.id == EvaluationResult.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(EvaluationResult.ts >= cutoff_low)
            .where(EvaluationResult.ts < cutoff_high)
        )
        if workspace_id is not None:
            stmt = stmt.where(Conversation.workspace_id == workspace_id)
        stmt = stmt.limit(_SCAN_LIMIT)
        rows = session.execute(stmt).all()
        return [(r[0], r[1]) for r in rows]

    if kind == "csat":
        from agentic_rag_project.feedback.models import FeedbackRating

        # LIKE → 1.0, DISLIKE → 0.0; NULL otherwise.
        value_expr = func.case(
            (Feedback.rating == FeedbackRating.LIKE.value, 1.0),
            (Feedback.rating == FeedbackRating.DISLIKE.value, 0.0),
            else_=None,
        )
        stmt = (
            select(Feedback.created_at.label("ts"), value_expr.label("value"))
            .where(Feedback.created_at >= cutoff_low)
            .where(Feedback.created_at < cutoff_high)
        )
        if workspace_id is not None:
            stmt = stmt.where(Feedback.workspace_id == workspace_id)
        stmt = stmt.limit(_SCAN_LIMIT)
        rows = session.execute(stmt).all()
        # Synthesize metric_name (only one: "csat_score")
        return [("csat_score", float(r.value)) for r in rows if r.value is not None]

    if kind == "dislike_attribution":
        stmt = (
            select(FeedbackCategory.key, Feedback.created_at.label("ts"))
            .select_from(FeedbackAttribution)
            .join(Feedback, Feedback.id == FeedbackAttribution.feedback_id)
            .join(FeedbackCategory, FeedbackCategory.id == FeedbackAttribution.category_id)
            .where(Feedback.created_at >= cutoff_low)
            .where(Feedback.created_at < cutoff_high)
        )
        if workspace_id is not None:
            stmt = stmt.where(Feedback.workspace_id == workspace_id)
        stmt = stmt.limit(_SCAN_LIMIT)
        rows = session.execute(stmt).all()
        # Synthesize value=1.0 (the count comes from row count per metric_name)
        return [(r[0], 1.0) for r in rows]

    raise ValueError(f"unknown kind: {kind!r}")


def _percentile(values: list[float], pct: float) -> float:
    """Pure-Python percentile (linear interpolation). Sorted ascending."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # Linear interpolation — matches PostgreSQL's percentile_cont default.
    rank = pct * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def baseline_for_metric(
    session: Session,
    *,
    kind: str,
    metric_name: str,
    workspace_id: uuid.UUID | None,
    now: _dt.datetime,
    cold_start_threshold: int = COLD_START_THRESHOLD,
) -> tuple[float, int] | None:
    """Compute the 7-day P50 baseline for `(kind, metric_name, workspace_id)`.

    Window: `[now - 7d, now - 1h]`. Excludes the most recent hour so
    the baseline doesn't see what we're about to call "current".

    Returns `(p50, sample_count)` or `None` when the sample count is
    below `cold_start_threshold` (insufficient data — caller skips).
    """
    cutoff_high = now - _dt.timedelta(hours=1)
    cutoff_low = now - _dt.timedelta(days=7)
    rows = _rows_in_window(
        session,
        kind=kind,
        workspace_id=workspace_id,
        cutoff_low=cutoff_low,
        cutoff_high=cutoff_high,
    )
    # Filter to the metric_name under inspection.
    values = [v for mn, v in rows if mn == metric_name]
    if len(values) < cold_start_threshold:
        logger.info(
            "drift baseline cold-start: kind=%s metric=%s ws=%s n=%d",
            kind,
            metric_name,
            workspace_id,
            len(values),
        )
        return None
    p50 = _percentile(values, 0.5)
    return p50, len(values)


__all__ = [
    "COLD_START_THRESHOLD",
    "baseline_for_metric",
    "_metric_names_for",
]
