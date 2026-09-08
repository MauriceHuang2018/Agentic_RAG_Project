"""Celery task that flushes the Redis audit buffer to PG (M4.3.1.e).

Registered on the project Celery app via `make_flush_audit_buffer_task()`,
called by the `audit-flush-buffer-5s` beat entry in
`doc_processor.celery_app.beat_schedule` (every 5 seconds).

Design notes:
  - Lazy imports for `_session_factory` and `celery_app` to avoid
    circular imports at module load time (mirrors the drift tasks pattern).
  - Failures requeue the popped payloads so a transient PG outage
    doesn't drop audit data (see `AuditService.flush_buffer`).
  - Beat schedule lives next to the other L0/L3 collectors so the
    cadence conversation stays in one file.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_flush_audit_buffer_task():
    """Bind the audit buffer flusher to the project's Celery app.

    Returns the registered Celery task so `doc_processor.celery_app`
    can call it from `beat_schedule`.
    """
    from agentic_rag_project.db.session import _session_factory
    from agentic_rag_project.doc_processor.celery_app import celery_app

    from .service import AuditService

    @celery_app.task(
        name="agentic_rag_project.audit.flush_audit_buffer",
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_backoff_max=60,
        retry_kwargs={"max_retries": 3},
    )
    def _task(batch_size: int = 100) -> int:
        # Redis client is module-scoped in api_gateway.dependencies
        # (lifespan-cached dict, not lru_cache; see lifespan fix 2026-08-25).
        from agentic_rag_project.api_gateway.dependencies import (
            get_redis_dependency,
        )

        redis_client = get_redis_dependency()
        service = AuditService(
            redis_client=redis_client,
            session_factory=_session_factory(),
        )
        try:
            written = service.flush_buffer(batch_size=batch_size)
        except Exception:
            logger.exception("audit_flush_buffer_task_error")
            raise
        if written:
            logger.warning(
                "audit_flush_wrote",
                extra={"written": written},
            )
        return written

    return _task


__all__ = ["make_flush_audit_buffer_task"]