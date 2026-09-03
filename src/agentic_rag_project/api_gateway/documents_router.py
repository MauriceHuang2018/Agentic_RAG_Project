"""Document upload + status endpoints.

DESIGN T1.5:
- POST /documents/upload  (multipart/form-data, ≤ storage_max_upload_mb)
- GET  /documents/{id}    (status + metadata)
- GET  /documents         (list workspace documents)

The upload creates a Document row in 'pending' status, persists the
file under storage_root, and enqueues parse_document_task. The API
returns immediately; the worker flips status to 'ready' | 'failed'.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_current_user,
)
from agentic_rag_project.config import get_settings
from agentic_rag_project.db.models.documents import Document
from agentic_rag_project.db.session import get_db
from agentic_rag_project.doc_processor.storage import (
    StorageError,
    save_upload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


def _enqueue_parse(document_id: str) -> None:
    """Module-level indirection so tests can swap the dispatcher.

    Production: forward to Celery's `.delay()` (async enqueue).
    Tests: replaced via `monkeypatch.setattr(router_module, '_enqueue_parse', ...)`.
    """
    from agentic_rag_project.doc_processor.tasks import parse_document_task

    parse_document_task.delay(document_id)

# Mirrors the `documents.format` CHECK constraint in the migration.
_SUPPORTED_FORMATS = {"pdf", "docx", "pptx", "xlsx", "md", "txt"}

# Format hints per extension; the explicit `format` form field wins.
_EXT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".md": "md",
    ".txt": "txt",
}


class UploadResponse(BaseModel):
    """Response body for POST /documents/upload."""

    document_id: str
    status: str
    name: str
    size_bytes: int


class DocumentStatusResponse(BaseModel):
    """Response body for GET /documents/{id}."""

    document_id: str
    status: str
    name: str
    format: str
    size_bytes: int
    last_error: str | None = None


class DocumentListItem(BaseModel):
    document_id: str
    name: str
    format: str
    status: str
    created_at: str


def _infer_format(filename: str) -> str | None:
    """Map a filename's extension to a supported format token."""
    lower = filename.lower()
    for ext, fmt in _EXT_FORMATS.items():
        if lower.endswith(ext):
            return fmt
    return None


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and enqueue async parsing",
)
def upload_document(
    file: UploadFile = File(...),
    ctx: UserContext = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> UploadResponse:
    """Persist an upload, create a Document row, dispatch parse task."""
    settings = get_settings()

    fmt = _infer_format(file.filename or "")
    if fmt is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file extension; supported: {sorted(_EXT_FORMATS)}",
        )

    # Read with a hard cap to enforce storage_max_upload_mb.
    max_bytes = settings.storage_max_upload_mb * 1024 * 1024
    file_bytes = file.file.read(max_bytes + 1)
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {settings.storage_max_upload_mb}MB",
        )
    if not file_bytes:
        raise HTTPException(status_code=400, detail="empty upload")

    document_id = str(uuid.uuid4())
    try:
        stored = save_upload(file_bytes, document_id, file.filename or "upload.bin")
    except StorageError as exc:
        logger.warning("save_upload failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Pick the user's first workspace as the document's owner scope.
    if not ctx.workspace_ids:
        raise HTTPException(
            status_code=400,
            detail="user is not a member of any workspace",
        )
    workspace_id = next(iter(ctx.workspace_ids))

    doc = Document(
        id=uuid.UUID(document_id),
        # `ctx.workspace_ids` is frozenset[uuid.UUID] (see
        # `api_gateway.dependencies.UserContext`) so the element is
        # already a UUID — do NOT call uuid.UUID(workspace_id) again,
        # that raised AttributeError 'UUID' object has no attribute
        # 'replace' on every upload (closed 2026-09-03; was the
        # follow-up tracked in memory under
        # "Documents upload UUID double-wrap 2026-09-02").
        workspace_id=workspace_id,
        owner_id=ctx.user_id,
        name=file.filename or "upload.bin",
        format=fmt,
        status="pending",
        metadata_={"size_bytes": stored.size_bytes},
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    # Enqueue parse task. Import lazily to avoid loading Celery on
    # every API request when the worker isn't running yet.
    from agentic_rag_project.doc_processor.tasks import parse_document_task

    _enqueue_parse(document_id)

    return UploadResponse(
        document_id=document_id,
        status="pending",
        name=doc.name,
        size_bytes=stored.size_bytes,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentStatusResponse,
    summary="Read document metadata + parse status",
)
def get_document_status(
    document_id: str,
    ctx: UserContext = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> DocumentStatusResponse:
    """Return the current status of a previously-uploaded document."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid document_id") from exc

    doc = session.get(Document, doc_uuid)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    # `ctx.workspace_ids` is frozenset[uuid.UUID] (see
    # `api_gateway.dependencies.UserContext`) so the elements are
    # already UUIDs — do NOT call `uuid.UUID(w)` again, that raised
    # AttributeError 'UUID' object has no attribute 'replace' on
    # every GET (closed 2026-09-03 alongside the upload-side fix
    # at line ~158; both were part of the same pre-existing
    # "Documents upload UUID double-wrap" defect tracked under
    # "Documents upload UUID double-wrap 2026-09-02" — name is
    # misleading; the bug spans read + write paths).
    if not ctx.is_super_admin and doc.workspace_id not in ctx.workspace_ids:
        raise HTTPException(status_code=403, detail="not authorized for this document")

    last_error = (doc.metadata_ or {}).get("last_error") if doc.metadata_ else None
    size_bytes = int((doc.metadata_ or {}).get("size_bytes", 0)) if doc.metadata_ else 0
    return DocumentStatusResponse(
        document_id=str(doc.id),
        status=doc.status,
        name=doc.name,
        format=doc.format,
        size_bytes=size_bytes,
        last_error=last_error,
    )


@router.get(
    "",
    response_model=list[DocumentListItem],
    summary="List documents in the caller's workspaces",
)
def list_documents(
    ctx: UserContext = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> list[DocumentListItem]:
    """Return all non-deleted documents in workspaces the user belongs to."""
    if ctx.is_super_admin:
        stmt = select(Document).where(Document.deleted_at.is_(None))
    else:
        workspace_uuids = [uuid.UUID(w) for w in ctx.workspace_ids]
        if not workspace_uuids:
            return []
        stmt = select(Document).where(
            Document.workspace_id.in_(workspace_uuids),
            Document.deleted_at.is_(None),
        )
    rows = session.execute(stmt).scalars().all()
    return [
        DocumentListItem(
            document_id=str(d.id),
            name=d.name,
            format=d.format,
            status=d.status,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in rows
    ]


def get_router() -> APIRouter:
    """Mountable router (used by api_gateway.__init__)."""
    return router