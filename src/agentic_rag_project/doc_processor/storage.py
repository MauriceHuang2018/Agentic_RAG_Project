"""Local-FS file storage for uploaded documents.

DESIGN 4.2 / 10.1: storage_root = ./data/documents by default; the
runtime container mounts the same path so workers can read what the
api-gateway wrote. S3/MinIO support is out of scope for phase1-mvp.

`save_upload(file_bytes, document_id, filename)` writes the bytes
under {storage_root}/{document_id}/{filename} and returns the
absolute path; the Celery worker reads from the same location.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agentic_rag_project.config import get_settings

logger = logging.getLogger(__name__)

# Filesystem-safe filename: alphanumerics, dot, dash, underscore.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(Exception):
    """Raised when storage operations fail."""


@dataclass
class StoredFile:
    """Result of a successful save."""

    absolute_path: Path
    size_bytes: int
    storage_root: Path


def sanitize_filename(filename: str) -> str:
    """Return a path-safe version of an uploaded filename."""
    cleaned = _SAFE_NAME.sub("_", filename.strip())
    return cleaned or "upload.bin"


def save_upload(
    file_bytes: bytes,
    document_id: str,
    filename: str,
    *,
    storage_root: str | Path | None = None,
) -> StoredFile:
    """Persist an upload under {storage_root}/{document_id}/{safe_name}.

    Creates intermediate directories if needed. Returns metadata so
    callers can persist the path + size on the Document row.
    """
    settings = get_settings()
    root = Path(storage_root) if storage_root is not None else Path(settings.storage_root)
    safe = sanitize_filename(filename)
    target_dir = root / document_id
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"could not create {target_dir}: {exc}") from exc
    target = target_dir / safe
    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise StorageError(f"could not write {target}: {exc}") from exc
    size = target.stat().st_size
    logger.info("stored upload %s (%d bytes) at %s", safe, size, target)
    return StoredFile(absolute_path=target, size_bytes=size, storage_root=root)


def resolve_document_file(document_id: str, *, storage_root: str | Path | None = None) -> Path:
    """Return the on-disk path for an uploaded document (single-file doc)."""
    settings = get_settings()
    root = Path(storage_root) if storage_root is not None else Path(settings.storage_root)
    doc_dir = root / document_id
    if not doc_dir.is_dir():
        raise StorageError(f"no upload found for document {document_id}")
    candidates = [p for p in doc_dir.iterdir() if p.is_file()]
    if not candidates:
        raise StorageError(f"document {document_id} directory is empty")
    return candidates[0]


def remove_document(document_id: str, *, storage_root: str | Path | None = None) -> None:
    """Best-effort delete of a document's storage directory."""
    settings = get_settings()
    root = Path(storage_root) if storage_root is not None else Path(settings.storage_root)
    doc_dir = root / document_id
    if doc_dir.exists():
        shutil.rmtree(doc_dir, ignore_errors=True)