"""Drift detector (T4.4) — public surface.

Re-exports the three modules so callers can `from agentic_rag_project
.drift import DriftDetector, make_drift_detect_task, severity_for`.
"""

from agentic_rag_project.drift.detector import DriftDetector
from agentic_rag_project.drift.severity import (
    HIGHER_IS_WORSE,
    drift_pct,
    is_regression,
    severity_for,
)
from agentic_rag_project.drift.tasks import make_drift_detect_task

__all__ = [
    "DriftDetector",
    "HIGHER_IS_WORSE",
    "drift_pct",
    "is_regression",
    "make_drift_detect_task",
    "severity_for",
]
