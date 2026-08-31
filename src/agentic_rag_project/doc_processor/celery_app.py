"""Celery application factory.

DESIGN 4.2: Celery is the async task bus. Broker + backend both point
at Redis (db 1 for broker, db 2 for results) so we don't contend with
the API gateway's application-level cache.

`celery_app` is exported so the docker-compose worker/beat commands can
do `celery -A agentic_rag_project.doc_processor.celery_app ...`.
"""

from __future__ import annotations

from celery import Celery

from agentic_rag_project.config import get_settings


def make_celery_app() -> Celery:
    """Build a Celery app wired to Redis broker + result backend."""
    settings = get_settings()
    app = Celery(
        "agentic_rag_project",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )
    app.conf.update(
        # Always retry once on transient infrastructure errors (DESIGN T1.5).
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_max_tasks_per_child=200,
        worker_prefetch_multiplier=1,
        # Keep results for 1 hour; long enough for API polling, short
        # enough to not accumulate state in Redis.
        result_expires=3600,
        # Serialize with JSON (safer than pickle for cross-version safety).
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        # T4.3 — beat schedule for L2 / L3 gauge refreshes.
        # Eval-quality: every 5 minutes (the underlying metric_name
        # series changes on eval-pipeline runs, not per-request).
        # Business-derived: every 1 minute (CSAT/ticket counts are
        # near-realtime UX signals).
        # T4.4 — drift detector runs every 15 min. Comparing 1h of
        # activity against a 7-day P50 baseline; 15 min granularity
        # keeps the baseline window fresh without doubling the DB
        # load of the L2/L3 collectors.
        beat_schedule={
            "metrics-refresh-eval": {
                "task": "agentic_rag_project.metrics.refresh_eval_gauges",
                "schedule": 300.0,  # 5 min
            },
            "metrics-refresh-business": {
                "task": "agentic_rag_project.metrics.refresh_business_gauges",
                "schedule": 60.0,  # 1 min
            },
            "drift-detect": {
                "task": "agentic_rag_project.drift.detect_drift",
                "schedule": 900.0,  # 15 min
            },
            # M4.3.1 — flush the Redis audit buffer to PG every 5 s.
            # Celery beat's minimum is 1 s; 5 s is the chosen balance
            # between freshness and Redis round-trips (DESIGN §4.4).
            "audit-flush-buffer-5s": {
                "task": "agentic_rag_project.audit.flush_audit_buffer",
                "schedule": 5.0,
            },
        },
    )
    return app


celery_app = make_celery_app()


# Import tasks so `@celery_app.task` decorators register on import.
def _register_tasks() -> None:
    from agentic_rag_project.doc_processor import tasks as _tasks  # noqa: F401

    # M4.3.1 — audit buffer flusher. Owns its own registration
    # because it lives outside `doc_processor.tasks` (architectural
    # boundary: `audit` is a top-level module, not a doc-processor
    # sub-module).
    from agentic_rag_project.audit import tasks as _audit_tasks  # noqa: F401

    _audit_tasks.make_flush_audit_buffer_task()


_register_tasks()