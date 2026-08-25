"""Collector — refreshes L2 / L3 gauges from the DB (T4.3).

DESIGN 4.7 — these aggregations are too expensive for the
request hot path (they touch `feedbacks`, `tickets`, and the
`evaluation_results` table). They're run on a Celery beat task:

  * `refresh_eval_gauges`     — every 5 min
  * `refresh_business_gauges` — every 1 min

Both functions are **synchronous** and use their own short-lived
session. No transactions are held open across the loop. Each
gauge is set independently — partial refresh is recoverable on
the next tick.

Tolerance: every query is wrapped in a per-table try/except so
that missing production tables (e.g. `documents.chunks` during
the prototype phase) don't crash the whole collector. The
collector logs and moves on; ops see an absent gauge on Grafana
instead of a crashloop.

Both tasks live in `doc_processor.tasks` (the project's Celery
app) and import the refresh functions from here.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentic_rag_project.feedback.models import (
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackRating,
    FeedbackTicket,
)
from agentic_rag_project.observability.registry import get_metrics

logger = logging.getLogger(__name__)


# Window defaults — overridable by the caller so unit tests
# don't need to fake the wall clock.
DEFAULT_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# L2 — evaluation quality gauges
# ---------------------------------------------------------------------------


# Metric names that the L2 collector surfaces. Matches the set
# recorded by `record_evaluation_results` in T4.1.
_EVAL_METRIC_NAMES: tuple[str, ...] = (
    "faithfulness",
    "citation_accuracy",
    "recall",
    "precision",
    "relevance",
)


def refresh_eval_gauges(
    session: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> int:
    """Refresh `eval_*` gauges from `evaluation_results`.

    Returns the number of gauge-series written. Zero means the
    evaluation_results table is unavailable OR has no rows in
    the window — both are non-fatal.
    """
    metrics = get_metrics()
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    written = 0

    try:
        # Local import — `evaluation_results` lives in db.models.audit
        # and isn't part of the T4.1 feedback namespace.
        from agentic_rag_project.db.models import (
            Conversation,
            EvaluationResult,
            Message,
        )
    except Exception:  # pragma: no cover — defensive
        logger.warning(
            "evaluation_results table unavailable; skipping L2 refresh"
        )
        return 0

    # Aggregate per (workspace_id, metric_name) over the window.
    try:
        stmt = (
            select(
                Conversation.workspace_id,
                EvaluationResult.metric_name,
                func.avg(EvaluationResult.value),
                func.count(EvaluationResult.id),
            )
            .join(Message, Message.id == EvaluationResult.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(EvaluationResult.ts >= cutoff)
            .group_by(Conversation.workspace_id, EvaluationResult.metric_name)
        )
        rows = session.execute(stmt).all()
    except SQLAlchemyError as exc:
        logger.warning("eval_gauges query failed: %s", exc)
        return 0

    for ws_id, metric_name, avg, count in rows:
        if not metric_name:
            continue
        ws_key = str(ws_id)
        # Workspace + eval_set='default' (the prototype stores eval
        # runs as JSONL files; the live metric_name series is what
        # we surface).
        if metric_name == "faithfulness":
            metrics.eval_faithfulness_score.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(float(avg))
            # Hallucination rate = 1 - faithfulness.
            metrics.eval_hallucination_rate.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(1.0 - float(avg))
        elif metric_name == "citation_accuracy":
            metrics.eval_citation_accuracy.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(float(avg))
        elif metric_name == "recall":
            metrics.eval_context_recall.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(float(avg))
        elif metric_name == "precision":
            metrics.eval_context_precision.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(float(avg))
        elif metric_name == "relevance":
            metrics.eval_answer_relevance.labels(
                workspace_id=ws_key, eval_set="default"
            ).set(float(avg))
        written += 1
    return written


# ---------------------------------------------------------------------------
# L3 — business-derived gauges
# ---------------------------------------------------------------------------


def refresh_business_gauges(
    session: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> int:
    """Refresh csat_score, feedback_dislike_rate, ticket counts.

    `kb_activation_rate` requires the production `documents.chunks`
    table; if it's unavailable (T4.3 prototype phase) we skip it
    rather than crashing the whole collector.
    """
    metrics = get_metrics()
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    written = 0

    # ---- workspace list ----
    try:
        ws_ids = [
            row[0]
            for row in session.execute(
                select(Feedback.workspace_id).distinct()
            ).all()
        ]
    except SQLAlchemyError as exc:
        logger.warning("L3 workspace list failed: %s", exc)
        return 0

    # ---- per-workspace: CSAT + dislike attribution rate ----
    for ws_id in ws_ids:
        try:
            like_count = session.execute(
                select(func.count(Feedback.id)).where(
                    Feedback.workspace_id == ws_id,
                    Feedback.rating == FeedbackRating.LIKE.value,
                    Feedback.created_at >= cutoff,
                )
            ).scalar_one()
            dislike_count = session.execute(
                select(func.count(Feedback.id)).where(
                    Feedback.workspace_id == ws_id,
                    Feedback.rating == FeedbackRating.DISLIKE.value,
                    Feedback.created_at >= cutoff,
                )
            ).scalar_one()
        except SQLAlchemyError as exc:
            logger.warning("L3 like/dislike count failed for %s: %s", ws_id, exc)
            continue

        total = like_count + dislike_count
        csat = like_count / total if total else 0.0
        metrics.csat_score.labels(workspace_id=str(ws_id)).set(csat)
        written += 1

        # Dislike attribution rate per category.
        try:
            per_cat = session.execute(
                select(
                    FeedbackCategory.key,
                    func.count(Feedback.id),
                )
                .join(
                    FeedbackAttribution,
                    FeedbackAttribution.category_id == FeedbackCategory.id,
                )
                .join(
                    Feedback,
                    Feedback.id == FeedbackAttribution.feedback_id,
                )
                .where(
                    Feedback.workspace_id == ws_id,
                    Feedback.rating == FeedbackRating.DISLIKE.value,
                    Feedback.created_at >= cutoff,
                )
                .group_by(FeedbackCategory.key)
            ).all()
        except SQLAlchemyError as exc:
            logger.warning("L3 dislike attribution failed for %s: %s", ws_id, exc)
            per_cat = []

        for cat_key, n in per_cat:
            rate = (n / dislike_count) if dislike_count else 0.0
            metrics.feedback_dislike_rate.labels(
                workspace_id=str(ws_id), category_key=cat_key
            ).set(rate)
            written += 1

    # ---- workspace-free: dislike attribution totals ----
    try:
        dislike_totals = session.execute(
            select(
                FeedbackCategory.key,
                func.count(Feedback.id),
            )
            .join(
                FeedbackAttribution,
                FeedbackAttribution.category_id == FeedbackCategory.id,
            )
            .join(
                Feedback,
                Feedback.id == FeedbackAttribution.feedback_id,
            )
            .where(
                Feedback.rating == FeedbackRating.DISLIKE.value,
                Feedback.created_at >= cutoff,
            )
            .group_by(FeedbackCategory.key)
        ).all()
        for cat_key, n in dislike_totals:
            metrics.dislike_attribution_count.labels(category_key=cat_key).set(n)
            written += 1
    except SQLAlchemyError as exc:
        logger.warning("L3 dislike totals failed: %s", exc)

    # ---- ticket counts per status ----
    try:
        ticket_totals = session.execute(
            select(FeedbackTicket.status, func.count(FeedbackTicket.id))
            .where(FeedbackTicket.status != "closed")
            .group_by(FeedbackTicket.status)
        ).all()
        for status_key, n in ticket_totals:
            metrics.open_tickets_by_status.labels(status=status_key).set(n)
            written += 1
    except SQLAlchemyError as exc:
        logger.warning("L3 ticket counts failed: %s", exc)

    # ---- KB activation rate (production-only; skip if missing) ----
    try:
        from agentic_rag_project.db.models import Chunk, Citation, Document

        # Re-query distinct workspace ids; Chunk lives in prod schema.
        prod_ws_ids = [
            row[0]
            for row in session.execute(select(Document.workspace_id).distinct()).all()
        ]
        for ws_id in prod_ws_ids:
            distinct_chunks = session.execute(
                select(func.count(func.distinct(Citation.chunk_id)))
                .join(Message := _lazy_msg(), Message.id == Citation.message_id)
                .where(Message.conversation_id.in_(
                    select(_lazy_conv().id).where(_lazy_conv().workspace_id == ws_id)
                ))
            ).scalar_one()
            total_chunks = session.execute(
                select(func.count(Chunk.id))
                .join(Document, Document.id == Chunk.document_id)
                .where(Document.workspace_id == ws_id)
            ).scalar_one()
            activation = (distinct_chunks / total_chunks) if total_chunks else 0.0
            metrics.kb_activation_rate.labels(workspace_id=str(ws_id)).set(activation)
            written += 1
    except Exception as exc:  # noqa: BLE001 — defensive on prod-table absence
        logger.info("L3 kb_activation_rate skipped: %s", exc)

    return written


def _lazy_conv():
    from agentic_rag_project.db.models import Conversation

    return Conversation


def _lazy_msg():
    from agentic_rag_project.db.models import Message

    return Message


# ---------------------------------------------------------------------------
# Celery wiring
# ---------------------------------------------------------------------------


def make_refresh_eval_task():
    """Build a Celery task bound to the project's Celery app."""
    from agentic_rag_project.doc_processor.celery_app import celery_app
    from agentic_rag_project.db.session import _session_factory

    @celery_app.task(name="agentic_rag_project.metrics.refresh_eval_gauges")
    def _task() -> int:
        with _session_factory()() as session:
            return refresh_eval_gauges(session)

    return _task


def make_refresh_business_task():
    """Build a Celery task bound to the project's Celery app."""
    from agentic_rag_project.doc_processor.celery_app import celery_app
    from agentic_rag_project.db.session import _session_factory

    @celery_app.task(name="agentic_rag_project.metrics.refresh_business_gauges")
    def _task() -> int:
        with _session_factory()() as session:
            return refresh_business_gauges(session)

    return _task


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "make_refresh_business_task",
    "make_refresh_eval_task",
    "refresh_business_gauges",
    "refresh_eval_gauges",
]
