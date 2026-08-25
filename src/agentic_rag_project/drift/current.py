"""Current-window computation for the drift_detector (T4.4).

For each `(kind, metric_name, workspace_id)` triple, fetch the
mean of values in the last 1 hour. Returns `None` when there is
no recent activity — the detector skips rather than fabricating
a "current" value.

Same dispatch table as `baseline.py`; the queries differ only in
the time window. Kept in a separate module so each function fits
on one screen and tests can target either independently.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _rows_in_current_window(
    session: Session,
    *,
    kind: str,
    workspace_id: uuid.UUID | None,
    cutoff_low: _dt.datetime,
    cutoff_high: _dt.datetime,
):
    """Yield (metric_name, value) rows in [cutoff_low, cutoff_high] for `kind`."""
    from agentic_rag_project.db.models import Conversation, EvaluationResult, Message
    from agentic_rag_project.feedback.models import (
        Feedback,
        FeedbackAttribution,
        FeedbackCategory,
        FeedbackRating,
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
        return [(r[0], r[1]) for r in session.execute(stmt).all()]

    if kind == "csat":
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
        rows = session.execute(stmt).all()
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
        rows = session.execute(stmt).all()
        return [(r[0], 1.0) for r in rows]

    raise ValueError(f"unknown kind: {kind!r}")


def current_for_metric(
    session: Session,
    *,
    kind: str,
    metric_name: str,
    workspace_id: uuid.UUID | None,
    now: _dt.datetime,
) -> tuple[float, int] | None:
    """Compute the mean of `metric_name` over the last 1 hour.

    Returns `(mean, sample_count)` or `None` if the window is empty.
    """
    cutoff_high = now
    cutoff_low = now - _dt.timedelta(hours=1)
    rows = _rows_in_current_window(
        session,
        kind=kind,
        workspace_id=workspace_id,
        cutoff_low=cutoff_low,
        cutoff_high=cutoff_high,
    )
    values = [v for mn, v in rows if mn == metric_name]
    if not values:
        return None
    return sum(values) / len(values), len(values)


__all__ = ["current_for_metric"]
