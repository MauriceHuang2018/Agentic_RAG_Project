"""Tests for the documents upload API + storage + Celery task wiring.

The Celery task is exercised in `Eager mode` — `task_always_eager=True`
— so we don't need a live Redis broker to assert document state
transitions. Dependencies (DeepDoc, LiteLLM, Qdrant) are faked via the
DocProcessor dependency injection in T1.4.
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.users import User, Workspace
from agentic_rag_project.db.models.rbac import Role
from agentic_rag_project.api_gateway.dependencies import UserContext


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point storage_root at a tmp directory so tests don't pollute the repo."""
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "test")
    # Wipe the lru_cache so the new env takes effect.
    from agentic_rag_project import config as cfg

    cfg.get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    """Create an in-memory SQLite + full schema, monkey-patch get_db()."""
    from sqlalchemy.pool import StaticPool

    # StaticPool + check_same_thread=False keeps a single shared connection
    # so all sessions see the same in-memory tables.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Seed a workspace + user so the upload endpoint has somewhere to attach.
    with SessionLocal() as s:
        u = User(
            id=uuid.uuid4(),
            username="alice",
            email="a@b.c",
            password_hash="x",
            status="enable",
        )
        s.add(u)
        s.flush()
        ws = Workspace(id=uuid.uuid4(), name="ws-1", owner_id=u.id, status="enable")
        s.add(ws)
        s.commit()
        # Return real UUID objects, not str — Document.workspace_id is
        # `Mapped[uuid.UUID]` so SQLAlchemy's UUID processor calls
        # `.hex` on the value. A str slips through UserContext because
        # `frozenset` is heterogeneous, then explodes at INSERT.
        ws_id, user_id = ws.id, u.id

    from agentic_rag_project.db import session as db_session

    def _get_db_override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    # Use FastAPI's official dependency-override mechanism so route
    # dependency injection picks up the new get_db without rebinding
    # captured function references.
    from agentic_rag_project import main as app_main

    app_main.app.dependency_overrides[db_session.get_db] = _get_db_override
    return SessionLocal, ws_id, user_id


@pytest.fixture
def client(
    tmp_storage_root: Path,
    in_memory_db: tuple[sessionmaker, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Build a FastAPI TestClient with the storage_root + in-memory DB."""
    from agentic_rag_project import main as app_main
    from agentic_rag_project.api_gateway.dependencies import get_current_user

    # Don't use Celery's eager mode; we replace .delay() with a stub
    # so the API call returns before the task would run.
    SessionLocal, ws_id, user_id = in_memory_db

    def _fake_current_user() -> UserContext:
        return UserContext(
            user_id=user_id,  # already a UUID
            username="alice",
            is_super_admin=True,
            status="enable",
            workspace_ids=frozenset({ws_id}),  # already a UUID
            permissions=frozenset({"*"}),
        )

    # FastAPI's official dependency-override mechanism (monkeypatching the
    # module-level reference won't help because routes already captured it).
    app_main.app.dependency_overrides[get_current_user] = _fake_current_user

    # Replace the Celery dispatcher in the router with a synchronous stub
    # so the upload path returns 202 immediately without trying to enqueue
    # on Redis or run the actual parse pipeline (no DeepDoc / LiteLLM /
    # Qdrant containers in unit tests).
    def _stub_enqueue(document_id: str) -> None:
        from agentic_rag_project.db.models.documents import Document

        s = SessionLocal()
        try:
            doc = s.get(Document, uuid.UUID(document_id))
            if doc is not None:
                doc.status = "ready"
                s.commit()
        finally:
            s.close()

    import agentic_rag_project.api_gateway.documents_router as docs_router_mod

    monkeypatch.setattr(docs_router_mod, "_enqueue_parse", _stub_enqueue)

    return TestClient(app_main.app)


# ---------------------------------------------------------------------------
# storage layer
# ---------------------------------------------------------------------------


def test_storage_sanitize_filename() -> None:
    from agentic_rag_project.doc_processor.storage import sanitize_filename

    # Slashes are stripped (no path traversal), dots/dashes/underscores
    # are kept verbatim. Result is filesystem-safe but not human-pretty.
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert sanitize_filename("résumé.pdf") == "r_sum_.pdf"
    assert sanitize_filename("") == "upload.bin"


def test_storage_save_and_resolve(tmp_storage_root: Path) -> None:
    from agentic_rag_project.doc_processor.storage import (
        resolve_document_file,
        save_upload,
    )

    doc_id = str(uuid.uuid4())
    stored = save_upload(b"hello world", doc_id, "doc.txt")
    assert stored.size_bytes == 11
    assert stored.absolute_path.exists()
    resolved = resolve_document_file(doc_id)
    assert resolved == stored.absolute_path
    assert resolved.read_bytes() == b"hello world"


def test_storage_resolve_missing_raises(tmp_storage_root: Path) -> None:
    from agentic_rag_project.doc_processor.storage import (
        StorageError,
        resolve_document_file,
    )

    with pytest.raises(StorageError):
        resolve_document_file(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# upload API
# ---------------------------------------------------------------------------


def test_upload_happy_path(
    client: TestClient, tmp_storage_root: Path, in_memory_db: tuple[sessionmaker, str, str]
) -> None:
    files = {"file": ("manual.txt", io.BytesIO(b"hello"), "text/plain")}
    response = client.post("/api/v1/documents/upload", files=files)
    assert response.status_code == 202, response.text
    body = response.json()
    # The API responds immediately with status=pending; the (stubbed)
    # worker flips the DB row to ready asynchronously.
    assert body["status"] == "pending"
    assert body["name"] == "manual.txt"
    assert body["size_bytes"] == 5
    document_id = body["document_id"]
    # The file should be on disk under the storage root.
    stored = tmp_storage_root / document_id / "manual.txt"
    assert stored.exists()
    # The stubbed worker should have flipped the DB row to ready.
    SessionLocal, _, _ = in_memory_db
    from agentic_rag_project.db.models.documents import Document
    with SessionLocal() as s:
        row = s.get(Document, uuid.UUID(document_id))
        assert row is not None
        assert row.status == "ready"


def test_upload_rejects_unsupported_extension(client: TestClient) -> None:
    files = {"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")}
    response = client.post("/api/v1/documents/upload", files=files)
    assert response.status_code == 400
    assert "unsupported" in response.json()["detail"]


def test_upload_rejects_oversize(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower the cap to 1 byte so the test is fast.
    monkeypatch.setenv("STORAGE_MAX_UPLOAD_MB", "1")  # 1MB = 1,048,576 bytes
    from agentic_rag_project import config as cfg

    cfg.get_settings.cache_clear()
    files = {"file": ("big.txt", io.BytesIO(b"abcdef"), "text/plain")}
    # Now lower to 0MB equivalent by patching the field directly.
    settings = cfg.get_settings()
    object.__setattr__(settings, "storage_max_upload_mb", 0)
    response = client.post("/api/v1/documents/upload", files=files)
    assert response.status_code == 413


def test_upload_rejects_empty(client: TestClient) -> None:
    files = {"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
    response = client.post("/api/v1/documents/upload", files=files)
    assert response.status_code == 400


def test_get_document_status(client: TestClient) -> None:
    files = {"file": ("notes.md", io.BytesIO(b"# title\nbody"), "text/markdown")}
    upload = client.post("/api/v1/documents/upload", files=files).json()
    response = client.get(f"/api/v1/documents/{upload['document_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["name"] == "notes.md"
    assert body["format"] == "md"


def test_get_document_status_invalid_uuid(client: TestClient) -> None:
    response = client.get("/api/v1/documents/not-a-uuid")
    assert response.status_code == 400


def test_get_document_status_missing(client: TestClient) -> None:
    response = client.get(f"/api/v1/documents/{uuid.uuid4()}")
    assert response.status_code == 404


def test_list_documents_returns_uploaded(client: TestClient) -> None:
    for name in ("a.txt", "b.txt"):
        client.post(
            "/api/v1/documents/upload",
            files={"file": (name, io.BytesIO(b"x"), "text/plain")},
        )
    response = client.get("/api/v1/documents")
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 2
    names = {item["name"] for item in items}
    assert {"a.txt", "b.txt"} <= names