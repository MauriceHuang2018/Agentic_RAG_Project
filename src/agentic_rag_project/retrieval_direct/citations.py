"""Citation persistence (T2.4 first half).

DESIGN 4.2 / TASK T2.4: every assistant message is persisted with the list
of source chunks it cited, so an evaluator can later answer "which
documents did the model lean on for this answer?". `document_name` is
denormalized at insert time so the citation row survives a document
rename without rewriting history.

`record_citations` is the single entry point — it takes the (already
saved) `message_id`, the `SearchResult`s returned by the retrieval
pipeline, and looks up `document_name` + `page_no` from the `chunks` /
`documents` tables in one batched query.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import Chunk, Citation, Document
from agentic_rag_project.retrieval_direct.search import SearchResult

logger = logging.getLogger(__name__)


class CitationError(Exception):
    """Raised when citation recording cannot proceed."""


def _chunk_uuid(chunk_id: str) -> uuid.UUID:
    """Mirror `doc_processor.indexer._chunk_uuid` — derive the PG UUID from a
    string chunk id (we use the same uuid5 namespace everywhere)."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"agentic-rag-project/{chunk_id}")


def _resolve_chunk_metadata(
    session: Session, chunk_uuids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, str, int | None]]:
    """Return `chunk_uuid -> (document_uuid, document_name, page_no)`.

    Performs one batched SELECT joining `chunks` and `documents` so the
    caller doesn't issue N round-trips. Missing chunks are silently
    skipped — the caller will still record citations for the chunks we
    did resolve, with a warning for the rest.
    """
    if not chunk_uuids:
        return {}
    stmt = (
        select(Chunk.id, Chunk.document_id, Chunk.position, Document.name)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(chunk_uuids))
    )
    out: dict[uuid.UUID, tuple[uuid.UUID, str, int | None]] = {}
    for row in session.execute(stmt).all():
        cid, did, position, name = row
        page_no: int | None = None
        if isinstance(position, dict):
            raw = position.get("page")
            if isinstance(raw, int):
                page_no = raw
            elif isinstance(raw, (str, float)):
                try:
                    page_no = int(raw)
                except (ValueError, TypeError):
                    page_no = None
        out[cid] = (did, name, page_no)
    return out


def record_citations(
    session: Session,
    *,
    message_id: uuid.UUID,
    search_results: list[SearchResult],
) -> list[Citation]:
    """Persist one Citation row per search hit and return them in order.

    Chunks that no longer exist (deleted between retrieval and insert)
    are skipped with a warning — we never raise on missing source data
    because the answer has already been generated and we don't want a
    citation-write failure to surface to the user.
    """
    if not search_results:
        return []

    chunk_uuids = [_chunk_uuid(r.chunk_id) for r in search_results]
    meta = _resolve_chunk_metadata(session, chunk_uuids)

    rows: list[Citation] = []
    for rank, (result, cid) in enumerate(zip(search_results, chunk_uuids, strict=True)):
        info = meta.get(cid)
        if info is None:
            logger.warning(
                "citation target missing in pg: chunk_id=%s (uuid=%s)",
                result.chunk_id,
                cid,
            )
            continue
        _, doc_name, page_no = info
        row = Citation(
            message_id=message_id,
            chunk_id=cid,
            document_name=doc_name,
            page_no=page_no,
            relevance_score=float(result.score),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows
