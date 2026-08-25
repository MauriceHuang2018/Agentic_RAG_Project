"""Celery tasks for async document ingestion.

`parse_document_task` is enqueued by the upload endpoint; it loads the
file from local storage, drives the doc-processor pipeline, and flips
the Document row's `status` field through the standard state machine:

    pending -> processing -> ready | failed

DESIGN T1.5: "上传失败重试 1 次" -> `autoretry_for=(Exception,)` with
`max_retries=1`. On second failure, we mark status='failed' so the API
can surface the error to the user.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from agentic_rag_project.config import get_settings
from agentic_rag_project.db.models.documents import Document
from agentic_rag_project.db.session import _session_factory
from agentic_rag_project.doc_processor.celery_app import celery_app
from agentic_rag_project.doc_processor.embedder import LiteLLMEmbedder
from agentic_rag_project.doc_processor.parser import DeepDocClient
from agentic_rag_project.doc_processor.storage import resolve_document_file

logger = logging.getLogger(__name__)


def _build_qdrant_client() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


def _open_session() -> Session:
    """Open a fresh SQLAlchemy session for this task.

    The session factory is module-level so connection pooling is
    shared across task invocations within a worker.
    """
    return _session_factory()()


def _update_document_status(document_id: str, status: str, error: str | None = None) -> None:
    """Flip the Document.status field for the given UUID (string form)."""
    session = _open_session()
    try:
        doc = session.get(Document, document_id)
        if doc is None:
            logger.warning("document %s vanished before status update", document_id)
            return
        doc.status = status
        if error is not None:
            doc.metadata_ = {**(doc.metadata_ or {}), "last_error": error}
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="agentic_rag_project.parse_document_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=1,
)
def parse_document_task(self: Any, document_id: str) -> dict[str, Any]:
    """Async pipeline: load -> parse -> chunk -> embed -> index -> ready."""
    settings = get_settings()
    logger.info("parse_document_task starting for %s", document_id)
    _update_document_status(document_id, "processing")

    try:
        file_path = resolve_document_file(document_id)
        parser = DeepDocClient()
        embedder = LiteLLMEmbedder()
        qdrant = _build_qdrant_client()
        session = _open_session()
        try:
            from agentic_rag_project.doc_processor import process_document

            result = process_document(
                file_path=file_path,
                qdrant=qdrant,
                session=session,
                parser=parser,
                embedder=embedder,
                collection=settings.qdrant_collection,
                document_id=document_id,
            )
        finally:
            parser.close()
            embedder.close()
            session.close()
    except (httpx.HTTPError, OSError, RuntimeError) as exc:
        logger.warning("parse_document_task failed for %s: %s", document_id, exc)
        if self.request.retries >= self.max_retries:
            _update_document_status(document_id, "failed", error=str(exc))
        raise

    _update_document_status(document_id, "ready")
    logger.info(
        "parse_document_task done %s: %d parents / %d children",
        document_id,
        result.parent_chunks_written,
        result.child_chunks_written,
    )
    return {
        "document_id": document_id,
        "parent_chunks": result.parent_chunks_written,
        "child_chunks": result.child_chunks_written,
        "collection": result.qdrant_collection,
    }


# ---------------------------------------------------------------------------
# Observability — T4.3
# ---------------------------------------------------------------------------
# Refresh L2 (eval quality) and L3 (business-derived) gauges on a
# Celery beat schedule. The functions live in
# `agentic_rag_project.observability.collector`; we just wrap them
# in Celery task objects here so they share the project's Celery
# app + session pool.


@celery_app.task(name="agentic_rag_project.metrics.refresh_eval_gauges")
def refresh_eval_gauges_task() -> int:
    """Beat entry point — every 5 minutes (see celery_app beat schedule)."""
    from agentic_rag_project.observability.collector import refresh_eval_gauges

    session = _open_session()
    try:
        return refresh_eval_gauges(session)
    finally:
        session.close()


@celery_app.task(name="agentic_rag_project.metrics.refresh_business_gauges")
def refresh_business_gauges_task() -> int:
    """Beat entry point — every 1 minute (see celery_app beat schedule)."""
    from agentic_rag_project.observability.collector import refresh_business_gauges

    session = _open_session()
    try:
        return refresh_business_gauges(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Drift detector — T4.4
# ---------------------------------------------------------------------------
# Run every 15 min. Wraps `drift.detector.DriftDetector.run()` so the
# detector logic stays out of the Celery module — easier to unit-test
# without spinning up a worker.


@celery_app.task(name="agentic_rag_project.drift.detect_drift")
def drift_detect_task() -> int:
    """Beat entry point — every 15 minutes (see celery_app beat schedule)."""
    from agentic_rag_project.drift.detector import DriftDetector

    session = _open_session()
    try:
        detector = DriftDetector(session)
        return detector.run()
    finally:
        session.close()