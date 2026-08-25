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
    """

    def __init__(
        self,
        *,
        parser: DeepDocClient | None = None,
        embedder: LiteLLMEmbedder | None = None,
        qdrant: QdrantClient | None = None,
        collection: str | None = None,
    ) -> None:
        self._parser = parser or DeepDocClient()
        self._embedder = embedder or LiteLLMEmbedder()
        self._qdrant = qdrant
        self._collection = collection

    def process(
        self,
        file_path: str | Path,
        *,
        session: Session,
        document_id: str | None = None,
    ) -> IndexResult:
        """End-to-end: file → parents + embedded children → Qdrant + PG.

        Caller owns the SQLAlchemy session's lifecycle (commit / rollback).
        """
        if self._qdrant is None:
            raise ValueError("DocProcessor requires a QdrantClient")
        doc_id = document_id or str(uuid.uuid4())
        parsed = _parse_document(file_path, self._parser)
        parents, children = chunk_parsed_doc(parsed, doc_id)
        if not children:
            logger.warning("document %s produced no children — nothing to index", doc_id)
            return index(
                parents=parents,
                embedded=[],
                document_id=doc_id,
                qdrant=self._qdrant,
                session=session,
                collection=self._collection,
            )
        embedded = self._embedder.embed_chunks(children)
        return index(
            parents=parents,
            embedded=embedded,
            document_id=doc_id,
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
    embedder: LiteLLMEmbedder | None = None,
    collection: str | None = None,
    document_id: str | None = None,
) -> IndexResult:
    """Module-level one-shot wrapper around DocProcessor.process()."""
    proc = DocProcessor(parser=parser, embedder=embedder, qdrant=qdrant, collection=collection)
    try:
        return proc.process(file_path, session=session, document_id=document_id)
    finally:
        proc.close()