"""Evaluation persistence (T4.1).

DESIGN 4.5: each `(message, metric)` pair becomes one row in
`evaluation_results`. Grafana (T4.3) aggregates these rows over a
sliding window; the drift detector (T4.4) reads the same rows.

`record_evaluation_results` is the single write entry point. It
inserts and flushes so the caller has the row ids immediately for
follow-up logging. We never UPDATE existing rows — a fresh score
is always a new row, so re-evaluating the same message twice keeps
both data points (the dashboard picks the latest via ORDER BY ts).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from agentic_rag_project.db.models.audit import EvaluationResult
from agentic_rag_project.evaluation.metrics import MetricResult

logger = logging.getLogger(__name__)


def record_evaluation_results(
    session: Session,
    *,
    message_id: uuid.UUID,
    results: list[MetricResult],
) -> list[EvaluationResult]:
    """Persist `results` for `message_id` and return the inserted rows.

    Empty `results` is a no-op (returns `[]`); this keeps caller code
    simple — no need to guard against an empty list at the call site.
    """
    if not results:
        return []
    rows: list[EvaluationResult] = []
    for r in results:
        row = EvaluationResult(
            message_id=message_id,
            metric_name=r.metric_name,
            value=float(r.value),
            details=dict(r.details),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


__all__ = [
    "record_evaluation_results",
]
