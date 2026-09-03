"""Pydantic data-transfer objects for the doc-processor pipeline.

A document moves through four stages, each stage producing its own DTO:

    file on disk
        | parser.parse()
        v
    ParsedDoc   (hierarchical: doc -> sections -> blocks)
        | chunker.chunk()
        v
    (list[ParentChunk], list[ChildChunk])
        | embedder.embed()
        v
    list[EmbeddedChunk]  (vector + sparse repr)
        | indexer.index()
        v
    IndexResult  (counts + ids)

DTOs are kept separate from ORM models — indexer translates DTOs into
SQLAlchemy `Chunk` rows and Qdrant points. This keeps the doc-processor
independent of persistence details (DESIGN 4.1, 4.2).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, NonNegativeInt

# ---- parsed stage ----------------------------------------------------------


class BoundingBox(BaseModel):
    """Bounding box around a content element (DeepDoc/RapidOCR compatible)."""

    x0: float
    y0: float
    x1: float
    y1: float


class ContentBlock(BaseModel):
    """One atomic element inside a parsed page (text, table cell, image, ...).

    `kind` covers the full DeepDoc DLA class space (0-6, 8). The `meta`
    dict carries per-kind auxiliary data — e.g. ``dla_bbox`` for figures
    whose text is just a bbox placeholder, or ``cells`` (2D matrix of
    cell strings) for tables whose TSR payload lacked ``html``. Kept as
    a generic dict instead of per-kind fields so the schema stays small
    and downstream code can opt in to whatever meta keys it cares about.
    """

    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal[
        "text", "table", "image", "heading", "list",
        "figure", "figure_caption", "table_caption", "reference", "equation",
    ]
    text: str = ""
    level: NonNegativeInt = 0  # heading depth (0 = body, 1..6 = h1..h6)
    page: NonNegativeInt = 0
    bbox: BoundingBox | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ParsedSection(BaseModel):
    """A section groups content blocks under one heading."""

    heading: str
    level: NonNegativeInt = 1
    blocks: list[ContentBlock] = Field(default_factory=list)
    # page range is derived from blocks; we cache for quick lookup
    page_start: NonNegativeInt = 0
    page_end: NonNegativeInt = 0


class ParsedDoc(BaseModel):
    """Output of the parser — a structured view of the source document."""

    document_name: str
    format: Literal["pdf", "docx", "pptx", "xlsx", "md", "txt"]
    sections: list[ParsedSection] = Field(default_factory=list)
    # fallback marker — true when RapidOCR was used (for observability)
    ocr_used: bool = False
    page_count: NonNegativeInt = 0


# ---- chunk stage ------------------------------------------------------------


class ParentChunk(BaseModel):
    """A larger context unit — typically one chapter / section."""

    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    # workspace_id is required (no default) so construction fails fast
    # when an upstream caller forgot to thread it. The chunker + the
    # Celery task both load it from the parent `Document.workspace_id`.
    workspace_id: str
    content: str
    section_heading: str
    page_start: NonNegativeInt = 0
    page_end: NonNegativeInt = 0
    child_ids: list[str] = Field(default_factory=list)


class ChildChunk(BaseModel):
    """A smaller retrieval unit — typically one paragraph or table cell."""

    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    # See ParentChunk.workspace_id — same rationale: required,
    # fail-fast at Pydantic validation when an upstream caller
    # drops it. The chunker + Celery task thread it from
    # Document.workspace_id (P0 / 2026-09-03).
    workspace_id: str
    parent_id: str
    content: str
    block_kind: Literal[
        "text", "table", "image", "heading", "list",
        "figure", "figure_caption", "table_caption", "reference", "equation",
    ] = "text"
    page: NonNegativeInt = 0


# ---- embed stage ------------------------------------------------------------


class EmbeddedChunk(BaseModel):
    """A child chunk plus its dense + sparse vector representations."""

    chunk_id: str
    document_id: str
    # See ParentChunk.workspace_id — same rationale. The embedder
    # mirrors the chunk's workspace_id onto both the EmbeddedChunk
    # attribute AND the Qdrant payload dict so ACL filters can read
    # it without re-joining to PG (P0 / 2026-09-03).
    workspace_id: str
    parent_id: str
    content: str
    dense_vector: list[float]
    # sparse_vector is a sparse dict {int token_id: weight} ready for Qdrant
    sparse_vector: dict[int, float] = Field(default_factory=dict)
    payload: dict = Field(default_factory=dict)


# ---- index stage ------------------------------------------------------------


class IndexResult(BaseModel):
    """Outcome of pushing embedded chunks into Qdrant + Postgres."""

    document_id: str
    parent_chunks_written: NonNegativeInt = 0
    child_chunks_written: NonNegativeInt = 0
    qdrant_collection: str
    pg_chunk_rows: NonNegativeInt = 0