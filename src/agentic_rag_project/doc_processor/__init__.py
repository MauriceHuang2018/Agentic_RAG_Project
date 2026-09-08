"""doc-processor — parse → chunk → embed → index.

Public API:
    DocProcessor(...)               # orchestrator with injectable deps
    doc_processor.process_document(...)  # one-shot helper
    parse_document(file_path)       # parser-only
    chunk_parsed_doc(parsed, id)    # chunker-only
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Callable

from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from agentic_rag_project.doc_processor.chunker import (
    DEFAULT_CHILD_MAX_CHARS,
    DEFAULT_PARENT_MAX_CHARS,
    chunk_parsed_doc,
)
from agentic_rag_project.doc_processor.celery_app import celery_app
from agentic_rag_project.doc_processor.embedder import (
    DENSE_DIM,
    LiteLLMEmbedder,
)
from agentic_rag_project.doc_processor.indexer import (
    IndexResult,
    index,
)
from agentic_rag_project.doc_processor.models import (
    ChildChunk,
    ContentBlock,
    EmbeddedChunk,
    ParentChunk,
    ParsedDoc,
    ParsedSection,
)
from agentic_rag_project.doc_processor.parser import (
    DeepDocClient,
    ParserError,
    parse_document as _parse_document,
    parse_document_with_router,
)

__all__ = [
    "DEFAULT_CHILD_MAX_CHARS",
    "DEFAULT_PARENT_MAX_CHARS",
    "DENSE_DIM",
    "ChildChunk",
    "ContentBlock",
    "DeepDocClient",
    "DocProcessor",
    "EmbeddedChunk",
    "IndexResult",
    "LiteLLMEmbedder",
    "ParentChunk",
    "ParsedDoc",
    "ParsedSection",
    "ParserError",
    "celery_app",
    "chunk_parsed_doc",
    "index",
    "parse_document",
    "process_document",
]

logger = logging.getLogger(__name__)


class DocProcessor:
    """Orchestrates parser → chunker → embedder → indexer.

    All dependencies are injectable so unit tests can pass fakes for
    the DeepDoc client, embedder, Qdrant client, and PG session.

    Parser injection has two compatible shapes:

    * ``parser=DeepDocClient(...)`` — legacy path. Calls ``client.parse()``
      on every file. Still the right choice for tests that subclass
      ``DeepDocClient`` to override ``parse()`` (see
      ``tests/test_doc_processor.py::FakeParser``).
    * ``parser_router=parse_document_with_router`` — the format
      dispatcher introduced in parser_router (DESIGN §2.5). Routes by
      extension; structured formats use in-process extractors and only
      scanned PDFs hit the visual path. Takes precedence over ``parser``
      when both are supplied.
    """

    def __init__(
        self,
        *,
        parser: DeepDocClient | None = None,
        parser_router: Callable[[str | Path], ParsedDoc] | None = None,
        embedder: LiteLLMEmbedder | None = None,
        qdrant: QdrantClient | None = None,
        collection: str | None = None,
    ) -> None:
        self._parser = parser or DeepDocClient()
        self._parser_router = parser_router
        self._embedder = embedder or LiteLLMEmbedder()
        self._qdrant = qdrant
        self._collection = collection

    def process(
        self,
        file_path: str | Path,
        *,
        session: Session,
        document_id: str | None = None,
        workspace_id: str,
    ) -> IndexResult:
        """End-to-end: file → parents + embedded children → Qdrant + PG.

        Caller owns the SQLAlchemy session's lifecycle (commit / rollback).
        `workspace_id` is required (no default) — every emitted chunk
        (ParentChunk / ChildChunk / EmbeddedChunk / PG row / Qdrant
        payload) carries it so the chat ACL filter can scope retrieval.
        P0 / 2026-09-03 — see docs/workspace_id_pipeline/.
        """
        if self._qdrant is None:
            raise ValueError("DocProcessor requires a QdrantClient")
        doc_id = document_id or str(uuid.uuid4())
        # Prefer the parser_router (format dispatcher) when injected; fall
        # back to the legacy DeepDocClient.parse() path otherwise. The router
        # path is what celery workers use post-2026-09-04; the legacy path
        # stays for unit tests that subclass DeepDocClient.
        if self._parser_router is not None:
            parsed = self._parser_router(file_path)
        else:
            parsed = _parse_document(file_path, self._parser)
        parents, children = chunk_parsed_doc(parsed, doc_id, workspace_id)
        if not children:
            logger.warning("document %s produced no children — nothing to index", doc_id)
            return index(
                parents=parents,
                embedded=[],
                document_id=doc_id,
                workspace_id=workspace_id,
                qdrant=self._qdrant,
                session=session,
                collection=self._collection,
            )
        embedded = self._embedder.embed_chunks(children)
        return index(
            parents=parents,
            embedded=embedded,
            document_id=doc_id,
            workspace_id=workspace_id,
            qdrant=self._qdrant,
            session=session,
            collection=self._collection,
        )

    def close(self) -> None:
        self._parser.close()
        self._embedder.close()


def parse_document(file_path: str | Path, client: DeepDocClient | None = None) -> ParsedDoc:
    """Stand-alone parser entrypoint; `client` is injectable for tests."""
    return _parse_document(file_path, client=client)


def process_document(
    file_path: str | Path,
    *,
    qdrant: QdrantClient,
    session: Session,
    parser: DeepDocClient | None = None,
    parser_router: Callable[[str | Path], ParsedDoc] | None = None,
    embedder: LiteLLMEmbedder | None = None,
    collection: str | None = None,
    document_id: str | None = None,
    workspace_id: str,
) -> IndexResult:
    """Module-level one-shot wrapper around DocProcessor.process().

    `workspace_id` is required (no default). It threads through to the
    chunker, embedder, and indexer so every emitted chunk — both the
    PG `chunks` row and the Qdrant payload — carries it. The chat
    ACL filter relies on this field being set on every retrievable
    chunk (P0 / 2026-09-03).

    Pass `parser_router` to route by extension through the format
    dispatcher; otherwise the legacy `parser.parse()` path is used
    (preserved for FakeParser-based unit tests).
    """
    proc = DocProcessor(
        parser=parser,
        parser_router=parser_router,
        embedder=embedder,
        qdrant=qdrant,
        collection=collection,
    )
    try:
        return proc.process(
            file_path,
            session=session,
            document_id=document_id,
            workspace_id=workspace_id,
        )
    finally:
        proc.close()