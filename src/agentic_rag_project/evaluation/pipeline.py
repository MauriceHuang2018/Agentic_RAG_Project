"""End-to-end evaluation pipeline (T4.1).

DESIGN 4.5 — runs an `EvalSet` through the chat service, scores
each (case, answer) pair with `EvalScorer`, persists per-metric
rows to `evaluation_results`, and emits a single `EvaluationReport`
the CLI / dashboard / drift-detector can consume.

Why a separate pipeline (vs. inlining in the CLI)?
  * The pipeline is reusable from Celery beat (T4.4) so weekly
    evals don't depend on a human running the CLI.
  * On-progress callback makes the long-running loop observable
    without coupling to a specific log format.
  * Failure isolation: a single bad case logs + records an error
    rather than aborting the whole batch — same policy as the
    per-metric isolation in `EvalScorer`.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from agentic_rag_project.chat import (
    ChatQueryRequest,
    ChatService,
    ChatServiceError,
)
from agentic_rag_project.evaluation.eval_set import EvalCase, load_eval_set
from agentic_rag_project.evaluation.metrics import (
    ALL_METRIC_NAMES,
    MetricResult,
)
from agentic_rag_project.evaluation.persistence import record_evaluation_results
from agentic_rag_project.evaluation.scorer import EvalScorer

logger = logging.getLogger(__name__)


@dataclass
class PerMetricStats:
    """First-order statistics over a single metric across N cases."""

    metric_name: str
    count: int = 0
    mean: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "metric_name": self.metric_name,
            "count": self.count,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass
class CaseOutcome:
    """What happened for one case — either scored or skipped."""

    case_id: str
    message_id: str | None = None
    metrics: list[MetricResult] = field(default_factory=list)
    error: str | None = None


@dataclass
class EvaluationReport:
    """Aggregate output of `EvaluationPipeline.run_all`."""

    total_cases: int = 0
    succeeded: int = 0
    failed: int = 0
    per_metric: dict[str, PerMetricStats] = field(default_factory=dict)
    outcomes: list[CaseOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "per_metric": {
                name: stats.to_dict() for name, stats in self.per_metric.items()
            },
            "outcomes": [
                {
                    "case_id": o.case_id,
                    "message_id": o.message_id,
                    "metrics": [
                        {"metric_name": m.metric_name, "value": m.value}
                        for m in o.metrics
                    ],
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


ProgressFn = Callable[[int, int, str], None]


class EvaluationPipeline:
    """Run a full eval set end-to-end and persist results."""

    def __init__(
        self,
        *,
        chat_service: ChatService,
        scorer: EvalScorer,
        session_factory: Callable[[], Session],
        default_user_id: uuid.UUID,
        default_workspace_id: uuid.UUID,
    ) -> None:
        self._chat_service = chat_service
        self._scorer = scorer
        self._session_factory = session_factory
        self._default_user_id = default_user_id
        self._default_workspace_id = default_workspace_id

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run_all(
        self,
        cases: Iterable[EvalCase],
        *,
        on_progress: ProgressFn | None = None,
    ) -> EvaluationReport:
        cases_list = list(cases)
        report = EvaluationReport(total_cases=len(cases_list))
        report.per_metric = {
            name: PerMetricStats(metric_name=name) for name in ALL_METRIC_NAMES
        }

        for idx, case in enumerate(cases_list, start=1):
            if on_progress is not None:
                try:
                    on_progress(idx, len(cases_list), case.case_id)
                except Exception as exc:
                    logger.warning("on_progress callback failed: %s", exc)
            outcome = self._run_one(case)
            report.outcomes.append(outcome)
            if outcome.error is None:
                report.succeeded += 1
                for metric in outcome.metrics:
                    stats = report.per_metric.get(metric.metric_name)
                    if stats is None:
                        continue
                    self._update_stats(stats, metric.value)
            else:
                report.failed += 1

        return report

    def run_from_path(
        self,
        path: str,
        *,
        on_progress: ProgressFn | None = None,
    ) -> EvaluationReport:
        return self.run_all(load_eval_set(path), on_progress=on_progress)

    # ------------------------------------------------------------------
    # single-case execution
    # ------------------------------------------------------------------

    def _run_one(self, case: EvalCase) -> CaseOutcome:
        outcome = CaseOutcome(case_id=case.case_id)
        try:
            request = ChatQueryRequest(query=case.query)
            user_id = self._default_user_id
            workspace_id = self._default_workspace_id
            if case.workspace_id:
                try:
                    workspace_id = uuid.UUID(case.workspace_id)
                except ValueError:
                    logger.warning(
                        "case %s has invalid workspace_id '%s' — using default",
                        case.case_id,
                        case.workspace_id,
                    )
            with self._session_factory() as session:
                response = self._chat_service.handle(
                    session=session,
                    request=request,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                session.commit()

            scored = self._scorer.score(
                case_id=case.case_id,
                query=case.query,
                answer=response.answer,
                retrieved_chunks=[c.model_dump() for c in response.citations],
                expected_answer=case.expected_answer,
                expected_chunk_ids=list(case.expected_chunk_ids),
            )
            message_id = uuid.UUID(response.message_id)
            with self._session_factory() as session:
                record_evaluation_results(
                    session,
                    message_id=message_id,
                    results=scored.metrics,
                )
                session.commit()

            outcome.message_id = response.message_id
            outcome.metrics = scored.metrics
        except ChatServiceError as exc:
            outcome.error = f"chat_service: {exc}"
            logger.warning("case %s failed at chat layer: %s", case.case_id, exc)
        except Exception as exc:
            outcome.error = f"unexpected: {exc}"
            logger.exception("case %s failed unexpectedly", case.case_id)
        return outcome

    # ------------------------------------------------------------------
    # stats helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _update_stats(stats: PerMetricStats, value: float) -> None:
        if stats.count == 0:
            stats.minimum = value
            stats.maximum = value
            stats.mean = value
        else:
            stats.minimum = min(stats.minimum, value)
            stats.maximum = max(stats.maximum, value)
            stats.mean = (
                stats.mean * stats.count + value
            ) / (stats.count + 1)
        stats.count += 1


# ---------------------------------------------------------------------------
# Aggregation helpers (used by T4.4 drift detector)
# ---------------------------------------------------------------------------


def aggregate_by_metric(outcomes: list[CaseOutcome]) -> dict[str, list[float]]:
    """Group all (case, metric) values by metric name. No stats — raw."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if o.error is not None:
            continue
        for m in o.metrics:
            grouped[m.metric_name].append(m.value)
    return dict(grouped)


__all__ = [
    "CaseOutcome",
    "EvaluationPipeline",
    "EvaluationReport",
    "PerMetricStats",
    "ProgressFn",
    "aggregate_by_metric",
]
