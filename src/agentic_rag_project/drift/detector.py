"""Drift detector orchestrator (T4.4).

One pass per Celery tick: for each `(kind, metric_name)` pair,
for each workspace + the global aggregate, compare the 1-hour
current window to the 7-day same-hour-of-day baseline. When the
absolute drift crosses the severity ladder (15% / 30%), write a
`DriftAlert` row and bump `drift_events_total{kind, metric_name,
severity, scope}`.

Tolerance matches the T4.3 collector: each `(kind, metric_name)`
iteration is wrapped in try/except so a single bad query doesn't
abort the whole tick. Partial progress is recoverable on the
next beat.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import DriftAlert, Workspace
from agentic_rag_project.drift.baseline import (
    _metric_names_for,
    baseline_for_metric,
)
from agentic_rag_project.drift.current import current_for_metric
from agentic_rag_project.drift.severity import severity_for
from agentic_rag_project.observability.registry import get_metrics

logger = logging.getLogger(__name__)


# Drift kinds tracked by the detector. Each maps to a set of
# metric_names (see `_metric_names_for`). Add a new kind here +
# in `baseline.py` / `current.py` query builders to extend.
_KINDS: Final[tuple[str, ...]] = (
    "eval_quality",
    "csat",
    "dislike_attribution",
)


class DriftDetector:
    """Run one detection pass. Reusable across ticks."""

    def __init__(
        self,
        session: Session,
        *,
        now: _dt.datetime | None = None,
    ) -> None:
        self._session = session
        self._now = now or _dt.datetime.now(_dt.timezone.utc)

    def _list_workspace_ids(self) -> list[uuid.UUID]:
        """All workspace ids seen in the recent eval window."""
        try:
            rows = self._session.execute(select(Workspace.id)).all()
            return [r[0] for r in rows]
        except Exception as exc:
            logger.warning("drift detector: workspace list failed: %s", exc)
            return []

    def _process_one(
        self,
        *,
        kind: str,
        metric_name: str,
        workspace_id: uuid.UUID | None,
        scope: str,
    ) -> DriftAlert | None:
        """Run baseline + current + severity for one triple; return row or None."""
        baseline_pair = baseline_for_metric(
            self._session,
            kind=kind,
            metric_name=metric_name,
            workspace_id=workspace_id,
            now=self._now,
        )
        if baseline_pair is None:
            # Cold start — skip silently.
            return None
        baseline_val, baseline_n = baseline_pair

        current_pair = current_for_metric(
            self._session,
            kind=kind,
            metric_name=metric_name,
            workspace_id=workspace_id,
            now=self._now,
        )
        if current_pair is None:
            # No recent activity — nothing to compare.
            return None
        current_val, current_n = current_pair

        severity, drift_pct_val = severity_for(
            metric_name=metric_name,
            baseline=baseline_val,
            current=current_val,
        )

        row = DriftAlert(
            workspace_id=workspace_id,
            kind=kind,
            metric_name=metric_name,
            scope=scope,
            category=metric_name,  # legacy column — mirror metric_name
            baseline=baseline_val,
            current=current_val,
            drift_pct=drift_pct_val,
            severity=severity,
        )
        try:
            self._session.add(row)
            self._session.flush()
        except Exception as exc:
            logger.warning(
                "drift detector: row insert failed kind=%s metric=%s: %s",
                kind,
                metric_name,
                exc,
            )
            self._session.rollback()
            return None

        # Bump the Prometheus counter — even if the row insert failed
        # earlier, we want the metric to reflect what was detected.
        try:
            metrics = get_metrics()
            metrics.drift_events_total.labels(
                kind=kind,
                metric_name=metric_name,
                severity=severity,
                scope=scope,
            ).inc()
        except Exception as exc:
            logger.warning(
                "drift detector: counter bump failed kind=%s metric=%s: %s",
                kind,
                metric_name,
                exc,
            )

        return row

    def run(self) -> int:
        """Run one detection pass. Returns number of rows written."""
        workspace_ids = self._list_workspace_ids()
        rows_written = 0

        for kind in _KINDS:
            for metric_name in _metric_names_for(kind):
                for ws_id in workspace_ids:
                    try:
                        row = self._process_one(
                            kind=kind,
                            metric_name=metric_name,
                            workspace_id=ws_id,
                            scope="workspace",
                        )
                        if row is not None:
                            rows_written += 1
                    except Exception as exc:
                        logger.warning(
                            "drift detector: tick failed kind=%s "
                            "metric=%s ws=%s: %s",
                            kind,
                            metric_name,
                            ws_id,
                            exc,
                        )
                        continue
                # Global aggregate row — workspace_id=None.
                try:
                    row = self._process_one(
                        kind=kind,
                        metric_name=metric_name,
                        workspace_id=None,
                        scope="global",
                    )
                    if row is not None:
                        rows_written += 1
                except Exception as exc:
                    logger.warning(
                        "drift detector: global tick failed "
                        "kind=%s metric=%s: %s",
                        kind,
                        metric_name,
                        exc,
                    )
                    continue

        try:
            self._session.commit()
        except Exception as exc:
            logger.warning("drift detector: commit failed: %s", exc)
            self._session.rollback()

        return rows_written


__all__ = ["DriftDetector", "_KINDS"]
