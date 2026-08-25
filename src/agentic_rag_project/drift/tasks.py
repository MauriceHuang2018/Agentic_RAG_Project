"""Celery task wiring for the drift detector (T4.4).

Mirrors the pattern in `observability/collector.py::make_refresh_*_task`:
lazy imports for `celery_app` + the session factory to avoid circular
imports at module load time.

The beat schedule entry is registered by `doc_processor.celery_app`
via `make_drift_detect_task()` — see the `drift-detect` entry in
`beat_schedule`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_drift_detect_task():
    """Bind the DriftDetector to the project's Celery app."""
    from agentic_rag_project.db.session import _session_factory
    from agentic_rag_project.doc_processor.celery_app import celery_app
    from agentic_rag_project.drift.detector import DriftDetector

    @celery_app.task(name="agentic_rag_project.drift.detect_drift")
    def _task() -> int:
        with _session_factory()() as session:
            detector = DriftDetector(session)
            return detector.run()

    return _task


__all__ = ["make_drift_detect_task"]
