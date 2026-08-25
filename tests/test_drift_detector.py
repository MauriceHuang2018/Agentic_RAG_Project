"""Tests for the T4.4 drift_detector.

Pure-function tests for `severity_for` (no DB) and orchestrator tests
for `DriftDetector` that monkeypatch the baseline / current query
helpers. This keeps the suite independent of PostgreSQL.

Each test resets the Prometheus metric singleton so the
`drift_events_total` counter starts at 0 — the singleton is shared
across the test process and we don't want cross-test contamination.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid

import pytest

from agentic_rag_project.db.models import DriftAlert
from agentic_rag_project.drift import severity_for
from agentic_rag_project.drift.detector import DriftDetector
from agentic_rag_project.drift.severity import (
    HIGHER_IS_WORSE,
    drift_pct,
    is_regression,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    """Minimal stand-in for SQLAlchemy's Result.all()."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """A do-everything fake session that records added rows + commit calls.

    `execute(stmt)` is intercepted by a hook the test sets to control
    what's returned. `add()` and `flush()` append to `added_rows`.
    `commit()` is a no-op that increments `commit_count`.
    """

    def __init__(self) -> None:
        self.added_rows: list[DriftAlert] = []
        self.committed = False
        self.rolled_back = False
        # Each execute() call pops from this list; tests pre-load it.
        self.execute_queue: list[list[tuple]] = []
        # Default rows for the workspace-list query (Workspace.id SELECT).
        # Tests override by appending to execute_queue before run().
        self.execute_default: list[tuple] = []

    def execute(self, stmt):
        if self.execute_queue:
            rows = self.execute_queue.pop(0)
        else:
            rows = self.execute_default
        return _FakeScalarResult(rows)

    def add(self, row):
        self.added_rows.append(row)

    def flush(self) -> None:
        # No-op; primary keys already populated by default= factory.
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Drop the metric singleton so each test starts from a clean counter."""
    from agentic_rag_project.observability import registry

    registry.reset_default_registry()
    yield
    registry.reset_default_registry()


def _seed_rows(metric_name: str, values: list[float], now: _dt.datetime) -> list[tuple]:
    """Build `(metric_name, value)` tuples inside the 1h current window."""
    return [(metric_name, v) for v in values]


def _seed_baseline_rows(metric_name: str, values: list[float]) -> list[tuple]:
    """Build `(metric_name, value)` tuples for the baseline window.

    `_rows_in_window` returns `(metric_name, value)` directly — the
    timestamp filter happens inside the SQL query, not in the helper,
    so tests that bypass the SQL layer just hand over the rows.
    """
    return [(metric_name, v) for v in values]


# ---------------------------------------------------------------------------
# severity_for — pure function tests
# ---------------------------------------------------------------------------


def test_drift_pct_zero_baseline_is_zero() -> None:
    """baseline == 0 → drift_pct = 0 (caller should treat as cold start)."""
    assert drift_pct(baseline=0.0, current=0.5) == 0.0
    assert drift_pct(baseline=0.0, current=-1.0) == 0.0


def test_drift_pct_signed() -> None:
    """Positive when current > baseline, negative when current < baseline."""
    assert drift_pct(baseline=1.0, current=0.5) == pytest.approx(-0.5)
    assert drift_pct(baseline=1.0, current=1.5) == pytest.approx(0.5)
    assert drift_pct(baseline=1.0, current=1.0) == 0.0


def test_is_regression_lower_is_worse_default() -> None:
    """Default metrics (lower is worse): drop = regression."""
    assert is_regression(metric_name="faithfulness", baseline=0.9, current=0.5) is True
    assert is_regression(metric_name="csat_score", baseline=0.9, current=1.0) is False


def test_is_regression_higher_is_worse() -> None:
    """Higher-is-worse metrics (hallucination, latency): rise = regression."""
    assert is_regression(metric_name="hallucination", baseline=0.1, current=0.3) is True
    assert is_regression(metric_name="hallucination", baseline=0.3, current=0.1) is False


def test_severity_for_warning_at_25_pct_drop() -> None:
    """Faithfulness 0.80 → 0.60 is a 25% drop → warning."""
    sev, pct = severity_for(metric_name="faithfulness", baseline=0.80, current=0.60)
    assert sev == "warning"
    assert pct == pytest.approx(-0.25)


def test_severity_for_critical_at_35_pct_drop() -> None:
    """Faithfulness 0.80 → 0.52 is a 35% drop → critical."""
    sev, pct = severity_for(metric_name="faithfulness", baseline=0.80, current=0.52)
    assert sev == "critical"
    assert pct == pytest.approx(-0.35)


def test_severity_for_info_below_15_pct() -> None:
    """Faithfulness 0.80 → 0.74 is a 7.5% drop → info (below warning)."""
    sev, pct = severity_for(metric_name="faithfulness", baseline=0.80, current=0.74)
    assert sev == "info"
    assert pct == pytest.approx(-0.075)


def test_severity_for_higher_is_worse_direction() -> None:
    """Hallucination rises by 50% → critical (higher-is-worse flip)."""
    sev, pct = severity_for(metric_name="hallucination", baseline=0.10, current=0.15)
    assert sev == "critical"
    assert pct == pytest.approx(0.5)


def test_severity_for_improvement_is_info() -> None:
    """Faithfulness rises (improvement) → info, even if huge."""
    sev, pct = severity_for(metric_name="faithfulness", baseline=0.5, current=1.0)
    assert sev == "info"
    assert pct == pytest.approx(1.0)


def test_higher_is_worse_includes_latency() -> None:
    """Latency drift (phase 2) is wired into the direction flag now."""
    assert "latency_p95" in HIGHER_IS_WORSE
    assert "hallucination" in HIGHER_IS_WORSE


# ---------------------------------------------------------------------------
# DriftDetector — orchestrator tests (with monkeypatched queries)
# ---------------------------------------------------------------------------


def _make_detector(
    *,
    baseline_results: dict[tuple[str, str, uuid.UUID | None], tuple[float, int] | None],
    current_results: dict[tuple[str, str, uuid.UUID | None], tuple[float, int] | None],
    workspace_ids: list[uuid.UUID],
) -> DriftDetector:
    """Build a detector with stubbed baseline / current / workspace lookup."""
    session = _FakeSession()
    # The detector first executes `SELECT Workspace.id` — supply our list.
    session.execute_default = [(ws,) for ws in workspace_ids]

    def _baseline_stub(*args, **kwargs):
        key = (kwargs["kind"], kwargs["metric_name"], kwargs["workspace_id"])
        return baseline_results.get(key)

    def _current_stub(*args, **kwargs):
        key = (kwargs["kind"], kwargs["metric_name"], kwargs["workspace_id"])
        return current_results.get(key)

    detector = DriftDetector(session)
    # Patch the imported helpers in detector's namespace.
    import agentic_rag_project.drift.detector as detector_mod

    detector_mod.baseline_for_metric = _baseline_stub  # type: ignore[assignment]
    detector_mod.current_for_metric = _current_stub  # type: ignore[assignment]
    return detector


def test_detector_skips_when_baseline_cold_start() -> None:
    """Cold-start (None from baseline_for_metric) → no row written."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_id): None,
            ("eval_quality", "faithfulness", None): None,
        },
        current_results={},
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    assert rows == 0
    assert len(detector._session.added_rows) == 0  # type: ignore[attr-defined]


def test_detector_skips_when_current_window_empty() -> None:
    """No recent activity (None from current_for_metric) → no row."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_id): (0.85, 100),
            ("eval_quality", "faithfulness", None): (0.85, 100),
        },
        current_results={
            ("eval_quality", "faithfulness", ws_id): None,
            ("eval_quality", "faithfulness", None): None,
        },
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    assert rows == 0


def test_detector_writes_critical_row_on_35pct_faithfulness_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Faithfulness baseline 0.85 → current 0.55 (35% drop) → critical row + counter bump."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_id): (0.85, 200),
            ("eval_quality", "faithfulness", None): (0.85, 200),
        },
        current_results={
            ("eval_quality", "faithfulness", ws_id): (0.55, 10),
            ("eval_quality", "faithfulness", None): (0.55, 10),
        },
        workspace_ids=[ws_id],
    )

    with caplog.at_level(logging.WARNING):
        rows = detector.run()

    # Per-workspace + global = 2 rows.
    assert rows == 2
    assert len(detector._session.added_rows) == 2  # type: ignore[attr-defined]

    severities = {r.severity for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert severities == {"critical"}

    # Counter should be at 2.0 (workspace + global, both critical).
    from agentic_rag_project.observability.registry import get_metrics

    metrics = get_metrics()
    val = metrics.drift_events_total.labels(
        kind="eval_quality",
        metric_name="faithfulness",
        severity="critical",
        scope="workspace",
    )._value.get()
    assert val == 1.0
    val_global = metrics.drift_events_total.labels(
        kind="eval_quality",
        metric_name="faithfulness",
        severity="critical",
        scope="global",
    )._value.get()
    assert val_global == 1.0


def test_detector_writes_info_row_for_improvement() -> None:
    """Faithfulness rises (improvement) → info row, no critical counter bump."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_id): (0.5, 100),
            ("eval_quality", "faithfulness", None): (0.5, 100),
        },
        current_results={
            ("eval_quality", "faithfulness", ws_id): (1.0, 10),
            ("eval_quality", "faithfulness", None): (1.0, 10),
        },
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    assert rows == 2
    severities = {r.severity for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert severities == {"info"}


def test_detector_writes_global_aggregate_row() -> None:
    """When at least one workspace has drift, the global aggregate row also fires."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "recall", ws_id): (0.85, 100),
            ("eval_quality", "recall", None): (0.85, 100),
        },
        current_results={
            ("eval_quality", "recall", ws_id): (0.55, 10),
            ("eval_quality", "recall", None): (0.55, 10),
        },
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    scopes = {r.scope for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert scopes == {"workspace", "global"}


def test_detector_handles_multiple_workspaces() -> None:
    """Each workspace contributes one row; the loop doesn't bail on the first one."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_a): (0.9, 100),
            ("eval_quality", "faithfulness", ws_b): (0.9, 100),
            ("eval_quality", "faithfulness", None): (0.9, 200),
        },
        current_results={
            ("eval_quality", "faithfulness", ws_a): (0.55, 10),
            ("eval_quality", "faithfulness", ws_b): (0.55, 10),
            ("eval_quality", "faithfulness", None): (0.55, 20),
        },
        workspace_ids=[ws_a, ws_b],
    )
    rows = detector.run()
    # 2 workspaces + 1 global = 3 rows.
    assert rows == 3
    workspace_scopes = [
        r for r in detector._session.added_rows if r.scope == "workspace"  # type: ignore[attr-defined]
    ]
    assert {r.workspace_id for r in workspace_scopes} == {ws_a, ws_b}


def test_detector_handles_higher_is_worse_metric() -> None:
    """Hallucination rising 50% (0.10 → 0.15) → critical."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "hallucination", ws_id): (0.10, 200),
            ("eval_quality", "hallucination", None): (0.10, 200),
        },
        current_results={
            ("eval_quality", "hallucination", ws_id): (0.15, 10),
            ("eval_quality", "hallucination", None): (0.15, 10),
        },
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    assert rows == 2
    severities = {r.severity for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert severities == {"critical"}
    # Drift_pct should be POSITIVE for higher-is-worse metrics.
    drift_pcts = {r.drift_pct for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert all(p > 0 for p in drift_pcts)


def test_detector_writes_warning_at_25pct_drop() -> None:
    """Faithfulness 0.80 → 0.60 → warning (not critical, not info)."""
    ws_id = uuid.uuid4()
    detector = _make_detector(
        baseline_results={
            ("eval_quality", "faithfulness", ws_id): (0.80, 100),
            ("eval_quality", "faithfulness", None): (0.80, 100),
        },
        current_results={
            ("eval_quality", "faithfulness", ws_id): (0.60, 10),
            ("eval_quality", "faithfulness", None): (0.60, 10),
        },
        workspace_ids=[ws_id],
    )
    rows = detector.run()
    severities = {r.severity for r in detector._session.added_rows}  # type: ignore[attr-defined]
    assert severities == {"warning"}


# ---------------------------------------------------------------------------
# Counter label cardinality (registry shape)
# ---------------------------------------------------------------------------


def test_counter_label_cardinality_is_bounded() -> None:
    """`drift_events_total` should have exactly 4 labels in the expected order."""
    from agentic_rag_project.observability.registry import get_metrics

    metrics = get_metrics()
    labelnames = frozenset(metrics.drift_events_total._labelnames)
    assert labelnames == frozenset(
        {"kind", "metric_name", "severity", "scope"}
    )


# ---------------------------------------------------------------------------
# Celery wiring — beat schedule + task registration
# ---------------------------------------------------------------------------


def test_celery_beat_includes_drift_detect_at_900s() -> None:
    """`beat_schedule` must contain `drift-detect` at 900-second cadence."""
    from agentic_rag_project.doc_processor.celery_app import celery_app

    beat_schedule = celery_app.conf.beat_schedule
    assert "drift-detect" in beat_schedule
    entry = beat_schedule["drift-detect"]
    assert entry["task"] == "agentic_rag_project.drift.detect_drift"
    assert entry["schedule"] == 900.0


def test_celery_task_is_registered() -> None:
    """Importing `tasks` registers the drift detect task on the celery_app."""
    from agentic_rag_project.doc_processor import celery_app
    from agentic_rag_project.doc_processor import tasks as _tasks  # noqa: F401

    assert "agentic_rag_project.drift.detect_drift" in celery_app.tasks