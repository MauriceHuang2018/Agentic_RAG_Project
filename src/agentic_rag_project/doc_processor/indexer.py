"""Qdrant + Postgres indexer for embedded chunks.

DESIGN 4.2 / 4.3: parent-child indexes in Qdrant; PG `chunks` table
holds the canonical metadata row. The Qdrant payload mirrors the
DB row so that retrieval-time filter passes (workspace / ACL) can
be applied without a second hop back to Postgres.

`index(embedded)` is the public entrypoint; it writes to both stores
inside a single function so the caller can wrap it in a Celery task.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from agentic_rag_project.config import get_settings
from agentic_rag_project.db.models.documents import Chunk
from agentic_rag_project.doc_processor.models import (
    EmbeddedChunk,
    IndexResult,
    ParentChunk,
)

logger = logging.getLogger(__name__)

# Per-point payload size cap (Qdrant recommendation); kept generous.
QDRANT_BATCH_SIZE = 256


class IndexerError(Exception):
    """Raised when Qdrant upsert or PG insert fails."""


def ensure_collection(client: QdrantClient, collection: str) -> None:
    """Create the collection with hybrid dense+sparse schema if absent."""
    if client.collection_exists(collection_name=collection):
        return
    client.create_collection(
        collection_name=collection,
        vectors_config={
            "dense": qmodels.VectorParams(size=1024, distance=qmodels.Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": qmodels.SparseVectorParams(index=qmodels.SparseIndexParams(on_disk=False))
        },
    )


def _chunk_uuid(chunk_id: str) -> uuid.UUID:
    """Convert our string chunk_id into a deterministic UUID for PG."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"agentic-rag-project/{chunk_id}")


def _qdrant_point_id(chunk_id: str) -> str:
    """Qdrant point id is a string uuid (derived from chunk_id)."""
    return str(_chunk_uuid(chunk_id))


def _content_hash(text: str) -> str:
    """sha256 hex digest for dedup index `chunks(content_hash)`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_point(embedded: EmbeddedChunk) -> qmodels.PointStruct:
    """Translate an EmbeddedChunk into a Qdrant point (with sparse + dense)."""
    sparse_indices = list(embedded.sparse_vector.keys())
    sparse_values = [float(embedded.sparse_vector[k]) for k in sparse_indices]
    return qmodels.PointStruct(
        id=_qdrant_point_id(embedded.chunk_id),
        vector={
            "dense": embedded.dense_vector,
            "sparse": qmodels.SparseVector(indices=sparse_indices, values=sparse_values),
        },
        payload={
            "chunk_id": embedded.chunk_id,
            "document_id": embedded.document_id,
            "parent_id": embedded.parent_id,
            "content": embedded.content,
            **embedded.payload,
        },
    )


def _write_chunks_row(
    session: Session,
    *,
    chunk_id: str,
    document_id: uuid.UUID,
    workspace_id: uuid.UUID,
    parent_chunk_id: uuid.UUID | None,
    content: str,
    chunk_index: int,
    page: int,
    section_path: str,
    is_parent: bool,
) -> None:
    """Upsert a single row into the canonical `chunks` table.

    Idempotent on the deterministic PK (``_chunk_uuid(chunk_id)``): a
    second call for the same chunk_id updates content/content_hash/
    position in place instead of appending a duplicate row. Identity
    fields (``document_id``, ``workspace_id``, ``parent_chunk_id``,
    ``is_parent``, ``chunk_index``) are preserved from the existing
    row when present — they are set on first insert and do not
    change on reindex.

    `workspace_id` is added as an identity field (P0 / 2026-09-03).
    A chunk's workspace is fixed at first index time and never
    changes on reindex — moving a document across workspaces is a
    separate operation that goes through a dedicated migration
    (out of scope here).
    """
    row_id = _chunk_uuid(chunk_id)
    content_hash = _content_hash(content)
    position = {"page": page, "section_path": section_path}
    # Build the values that would be inserted on first run.
    insert_values = {
        "id": row_id,
        "document_id": document_id,
        "workspace_id": workspace_id,
        "parent_chunk_id": parent_chunk_id,
        "chunk_index": chunk_index,
        "content": content,
        "content_hash": content_hash,
        "is_parent": is_parent,
        "position": position,
    }
    # On conflict: refresh content-derived fields only. Identity fields
    # (document_id, workspace_id, parent_chunk_id, is_parent,
    # chunk_index) keep their original value — reindexing the same
    # chunk must not change which document/parent/workspace it
    # belongs to or its position in the chunk list.
    stmt = pg_insert(Chunk).values(**insert_values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chunk.id],
        set_={
            "content": stmt.excluded.content,
            "content_hash": stmt.excluded.content_hash,
            "position": stmt.excluded.position,
        },
    )
    session.execute(stmt)


def index(
    *,
    parents: list[ParentChunk],
    embedded: list[EmbeddedChunk],
    document_id: str,
    workspace_id: str,
    qdrant: QdrantClient,
    session: Session,
    collection: str | None = None,
) -> IndexResult:
    """Write parents + embedded children into Qdrant and Postgres.

    Parents are stored as PG rows only (no vector) — the two-stage
    retrieval path joins back to PG by parent_id. Children carry both
    a dense and sparse vector and are the unit of retrieval.

    `workspace_id` is threaded through to every PG row written here
    and was already on the Qdrant payload via EmbeddedChunk.payload
    (set by LiteLLMEmbedder.embed_chunks). Both stores therefore have
    workspace_id set on every chunk, which the chat ACL filter
    reads at retrieval time. P0 / 2026-09-03.
    """
    settings = get_settings()
    coll = collection or settings.qdrant_collection
    ensure_collection(qdrant, coll)

    # `document_id` here is the canonical Document row UUID returned by
    # the upload endpoint (the FK target on `chunks.document_id`). It
    # MUST be the literal UUID, not a hash of it — `_chunk_uuid` would
    # re-hash the string into a different v5 UUID that doesn't exist in
    # `documents`, breaking the FK and causing retry storms.
    # (Bug surfaced 2026-09-04: P0 wiring forwarded `document_id` from
    # Celery unchanged, but indexer.apply_uuid5() shadowed it.)
    doc_uuid = uuid.UUID(document_id)
    ws_uuid = uuid.UUID(workspace_id)

    # 1. upsert children into Qdrant
    points = [_build_point(e) for e in embedded]
    if points:
        for start in range(0, len(points), QDRANT_BATCH_SIZE):
            batch = points[start : start + QDRANT_BATCH_SIZE]
            try:
                qdrant.upsert(collection_name=coll, points=batch, wait=True)
            except Exception as exc:  # qdrant-client raises broad exceptions
                raise IndexerError(f"qdrant upsert failed: {exc}") from exc

    # 2. insert PG rows for parents + children
    try:
        # Pass 1: parents — flush so children's FK to parents resolves.
        for parent_index, parent in enumerate(parents):
            _write_chunks_row(
                session,
                chunk_id=parent.chunk_id,
                document_id=doc_uuid,
                workspace_id=ws_uuid,
                parent_chunk_id=None,
                content=parent.content,
                chunk_index=parent_index,
                page=parent.page_start,
                section_path=parent.section_heading,
                is_parent=True,
            )
        session.flush()

        # Pass 2: children — each child's parent_chunk_id is the parent's PG UUID.
        for child_index, child in enumerate(embedded):
            parent_uuid = _chunk_uuid(child.parent_id)
            page = int(child.payload.get("page", 0))
            _write_chunks_row(
                session,
                chunk_id=child.chunk_id,
                document_id=doc_uuid,
                workspace_id=ws_uuid,
                parent_chunk_id=parent_uuid,
                content=child.content,
                chunk_index=len(parents) + child_index,
                page=page,
                section_path=str(child.payload.get("block_kind", "text")),
                is_parent=False,
            )
        session.commit()
    except Exception as exc:
        session.rollback()
        raise IndexerError(f"postgres insert failed: {exc}") from exc

    return IndexResult(
        document_id=document_id,
        parent_chunks_written=len(parents),
        child_chunks_written=len(embedded),
        qdrant_collection=coll,
        pg_chunk_rows=len(parents) + len(embedded),
    )