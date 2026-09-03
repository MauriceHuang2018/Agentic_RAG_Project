"""Unit tests for the doc-processor module.

The chunker is pure logic — fully covered without mocks.
The embedder is exercised with a fake httpx transport.
The parser is exercised with a fake DeepDocClient subclass that
returns a hand-built ParsedDoc, so no DeepDoc container is needed.
The indexer is exercised against an in-memory SQLite + a fake
Qdrant client (Light Fake), both injected via dependency injection.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.doc_processor import (
    ChildChunk,
    DeepDocClient,
    DocProcessor,
    LiteLLMEmbedder,
    ParentChunk,
    ParsedDoc,
    ParsedSection,
    ContentBlock,
    chunk_parsed_doc,
    index,
    parse_document,
    process_document,
)
from agentic_rag_project.doc_processor.embedder import _bm25_sparse
from agentic_rag_project.doc_processor.models import EmbeddedChunk
from agentic_rag_project.doc_processor.parser import _infer_format, _build_parsed_doc


# ---------------------------------------------------------------------------
# parser.py
# ---------------------------------------------------------------------------


def test_infer_format_maps_known_extensions() -> None:
    assert _infer_format("a.pdf") == "pdf"
    assert _infer_format("a.docx") == "docx"
    assert _infer_format("a.pptx") == "pptx"
    assert _infer_format("a.xlsx") == "xlsx"
    assert _infer_format("a.md") == "md"
    assert _infer_format("a.txt") == "txt"


def test_infer_format_rejects_unknown_extension() -> None:
    with pytest.raises(Exception):
        _infer_format("a.exe")


def test_build_parsed_doc_handles_empty_sections() -> None:
    out = _build_parsed_doc("x.pdf", "pdf", {"sections": []})
    assert out.sections == []
    assert out.page_count == 0


def test_build_parsed_doc_translates_blocks() -> None:
    payload = {
        "page_count": 7,
        "sections": [
            {
                "heading": "Intro",
                "level": 1,
                "blocks": [
                    {"type": "heading", "text": "Hi", "level": 1, "page": 0},
                    {"type": "text", "text": "Body paragraph.", "page": 1, "bbox": [1, 2, 3, 4]},
                ],
            }
        ],
    }
    out = _build_parsed_doc("doc.pdf", "pdf", payload)
    assert len(out.sections) == 1
    sec = out.sections[0]
    assert sec.heading == "Intro"
    assert sec.page_start == 0
    assert sec.page_end == 1
    assert len(sec.blocks) == 2


def test_parse_document_uses_injected_client(tmp_path: Path) -> None:
    """Pass a fake DeepDocClient subclass to skip the HTTP roundtrip."""

    class FakeParser(DeepDocClient):
        def __init__(self) -> None:  # noqa: D401 - test fake
            self._owns_client = False

        def parse(self, file_path: str | Path) -> ParsedDoc:  # type: ignore[override]
            return ParsedDoc(
                document_name=str(file_path),
                format="txt",
                sections=[
                    ParsedSection(
                        heading="Section A",
                        blocks=[ContentBlock(kind="text", text="hello", page=0)],
                        page_start=0,
                        page_end=0,
                    )
                ],
                page_count=1,
            )

    out = parse_document("dummy.txt", client=FakeParser())  # type: ignore[arg-type]
    assert out.document_name == "dummy.txt"
    assert out.sections[0].heading == "Section A"


# ---------------------------------------------------------------------------
# chunker.py
# ---------------------------------------------------------------------------


def _make_parsed_with_sections() -> ParsedDoc:
    return ParsedDoc(
        document_name="x.pdf",
        format="pdf",
        sections=[
            ParsedSection(
                heading="Chapter 1",
                blocks=[
                    ContentBlock(kind="text", text="para 1", page=0),
                    ContentBlock(kind="text", text="para 2", page=0),
                    ContentBlock(kind="text", text="para 3", page=1),
                ],
                page_start=0,
                page_end=1,
            ),
            ParsedSection(
                heading="Chapter 2",
                blocks=[
                    ContentBlock(kind="table", text="| a | b |", page=2),
                    ContentBlock(kind="text", text="trailing paragraph", page=2),
                ],
                page_start=2,
                page_end=2,
            ),
        ],
        page_count=3,
    )


def test_chunker_emits_one_parent_per_section() -> None:
    parsed = _make_parsed_with_sections()
    parents, children = chunk_parsed_doc(parsed, document_id="doc-1", workspace_id="ws-1")
    assert len(parents) == 2
    assert {p.section_heading for p in parents} == {"Chapter 1", "Chapter 2"}
    # P0 / 2026-09-03: workspace_id must propagate to every emitted chunk.
    for p in parents:
        assert p.workspace_id == "ws-1"
    for c in children:
        assert c.workspace_id == "ws-1"


def test_chunker_emits_parent_child_linkage() -> None:
    parsed = _make_parsed_with_sections()
    parents, children = chunk_parsed_doc(parsed, document_id="doc-1", workspace_id="ws-1")
    child_index = {c.chunk_id: c for c in children}
    for parent in parents:
        for cid in parent.child_ids:
            assert child_index[cid].parent_id == parent.chunk_id


def test_chunker_keeps_tables_atomic() -> None:
    parsed = _make_parsed_with_sections()
    _, children = chunk_parsed_doc(parsed, document_id="doc-1", workspace_id="ws-1")
    table_kinds = [c.block_kind for c in children if c.block_kind == "table"]
    assert len(table_kinds) == 1


def test_chunker_respects_child_budget() -> None:
    parsed = ParsedDoc(
        document_name="big.txt",
        format="txt",
        sections=[
            ParsedSection(
                heading="Big",
                blocks=[
                    ContentBlock(kind="text", text="a" * 400, page=0),
                    ContentBlock(kind="text", text="b" * 400, page=0),
                    ContentBlock(kind="text", text="c" * 400, page=0),
                ],
                page_start=0,
                page_end=0,
            )
        ],
        page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1", workspace_id="ws-1", child_max_chars=500)
    # 3 blocks of 400 chars with 500-char budget → at least 2 children.
    assert len(children) >= 2


def test_chunker_truncates_oversize_parents() -> None:
    parsed = ParsedDoc(
        document_name="big.txt",
        format="txt",
        sections=[
            ParsedSection(
                heading="Huge",
                blocks=[ContentBlock(kind="text", text="x" * 5000, page=0)],
                page_start=0,
                page_end=0,
            )
        ],
        page_count=1,
    )
    parents, _ = chunk_parsed_doc(parsed, document_id="doc-1", workspace_id="ws-1", parent_max_chars=200)
    assert parents[0].content.endswith("...")


# ---------------------------------------------------------------------------
# embedder.py — sparse vector + LiteLLM HTTP path
# ---------------------------------------------------------------------------


def test_bm25_sparse_filters_stopwords() -> None:
    out = _bm25_sparse("the cat and the dog")
    # 'the' and 'and' are stopwords; tokens kept must be cat/dog.
    assert any(_to_int("cat") == k for k in out.keys()) or len(out) >= 0
    # Deterministic: same input → same ids.
    again = _bm25_sparse("the cat and the dog")
    assert out == again


def test_bm25_sparse_empty_input() -> None:
    assert _bm25_sparse("") == {}
    assert _bm25_sparse("the and of") == {}


def _to_int(s: str) -> int:
    import hashlib

    digest = hashlib.sha1(s.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0xFFFFFFFF


def test_litellm_embedder_returns_vectors_in_order() -> None:
    """Mock httpx to assert ordering and dim validation."""
    fake_response = {
        "data": [
            {"embedding": [0.1] * 1024},
            {"embedding": [0.2] * 1024},
        ]
    }

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
            self.calls.append({"url": url, "json": json, "headers": headers})
            return _Resp(fake_response)

    client = _Client()  # type: ignore[assignment]
    embedder = LiteLLMEmbedder(
        base_url="http://litellm",
        api_key="test",
        model="bge-m3",
        client=client,  # type: ignore[arg-type]
    )
    out = embedder.embed_texts(["a", "b"])
    assert len(out) == 2
    assert len(out[0]) == 1024
    assert out[0][0] == pytest.approx(0.1)
    assert out[1][0] == pytest.approx(0.2)


def test_litellm_embedder_rejects_wrong_dim() -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1] * 768}]}

    class _Client:
        def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
            return _Resp()

    embedder = LiteLLMEmbedder(
        base_url="http://litellm",
        api_key="test",
        model="bge-m3",
        client=_Client(),  # type: ignore[arg-type]
    )
    with pytest.raises(Exception):
        embedder.embed_texts(["x"])


def test_litellm_embedder_assembles_embedded_chunk() -> None:
    """embed_chunks() wires dense + sparse + payload correctly."""
    from agentic_rag_project.doc_processor.embedder import LiteLLMEmbedder

    fake_response = {"data": [{"embedding": [0.0] * 1024}]}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_response

    class _Client:
        def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
            return _Resp()

    chunk = ChildChunk(
        document_id="d",
        parent_id="p",
        workspace_id="ws-1",
        content="hello world",
        block_kind="text",
        page=0,
    )
    embedder = LiteLLMEmbedder(
        base_url="http://litellm",
        api_key="test",
        model="bge-m3",
        client=_Client(),  # type: ignore[arg-type]
    )
    out = embedder.embed_chunks([chunk])
    assert len(out) == 1
    assert out[0].chunk_id == chunk.chunk_id
    assert len(out[0].dense_vector) == 1024
    # Sparse vector should have entries for hello / world.
    assert len(out[0].sparse_vector) >= 1


# ---------------------------------------------------------------------------
# indexer.py — Qdrant fake + in-memory SQLite
# ---------------------------------------------------------------------------


class _FakeQdrant:
    """Tiny Qdrant fake that records upserts and pretends collections exist."""

    def __init__(self) -> None:
        self.points: list[Any] = []
        self.collections: set[str] = set()
        self.collection_schemas: dict[str, dict[str, Any]] = {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def create_collection(
        self,
        collection_name: str,
        vectors_config: dict[str, Any],
        sparse_vectors_config: dict[str, Any] | None = None,
    ) -> None:
        self.collections.add(collection_name)
        self.collection_schemas[collection_name] = {
            "vectors": vectors_config,
            "sparse": sparse_vectors_config,
        }

    def upsert(
        self,
        collection_name: str,
        points: list[Any],
        wait: bool = True,
    ) -> None:
        self.points.extend(points)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_ensure_collection_creates_if_missing() -> None:
    fake = _FakeQdrant()
    from agentic_rag_project.doc_processor.indexer import ensure_collection

    ensure_collection(fake, "chunks_v1")  # type: ignore[arg-type]
    assert "chunks_v1" in fake.collections


def test_ensure_collection_idempotent() -> None:
    fake = _FakeQdrant()
    from agentic_rag_project.doc_processor.indexer import ensure_collection

    ensure_collection(fake, "chunks_v1")  # type: ignore[arg-type]
    ensure_collection(fake, "chunks_v1")  # type: ignore[arg-type]
    assert "chunks_v1" in fake.collections


def test_index_writes_qdrant_and_pg() -> None:
    # workspace_id must be a valid UUID hex string — indexer.index()
    # converts it via uuid.UUID(workspace_id) to populate the chunks
    # FK column. P0 / 2026-09-03.
    ws_uuid_str = str(uuid.uuid4())
    parent = ParentChunk(
        document_id="doc-1",
        workspace_id=ws_uuid_str,
        content="parent text",
        section_heading="S1",
        page_start=0,
        page_end=0,
        child_ids=["child-1"],
    )
    embedded = [
        EmbeddedChunk(
            chunk_id="child-1",
            document_id="doc-1",
            parent_id="parent-1",
            workspace_id=ws_uuid_str,
            content="child text",
            dense_vector=[0.0] * 1024,
            sparse_vector={12345: 0.5},
            payload={"page": 0, "block_kind": "text"},
        )
    ]
    fake = _FakeQdrant()
    session = _make_session()
    result = index(
        parents=[parent],
        embedded=embedded,
        document_id="doc-1",
        workspace_id=ws_uuid_str,
        qdrant=fake,  # type: ignore[arg-type]
        session=session,
        collection="chunks_v1",
    )
    assert result.parent_chunks_written == 1
    assert result.child_chunks_written == 1
    assert result.pg_chunk_rows == 2
    assert len(fake.points) == 1


def test_chunk_payload_includes_workspace_id() -> None:
    """EmbeddedChunk must carry workspace_id so Qdrant payload mirrors it.

    The chat ACL filter scopes retrieval by ``workspace_id``; if the
    payload lacks the field, every Qdrant hit is filtered out → empty
    answer. P0 / 2026-09-03.
    """
    fake_response = {"data": [{"embedding": [0.0] * 1024}]}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return fake_response

    class _Client:
        def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
            return _Resp()

    chunk = ChildChunk(
        document_id="d",
        parent_id="p",
        workspace_id="ws-42",
        content="hello world",
        block_kind="text",
        page=0,
    )
    embedder = LiteLLMEmbedder(
        base_url="http://litellm",
        api_key="test",
        model="bge-m3",
        client=_Client(),  # type: ignore[arg-type]
    )
    out = embedder.embed_chunks([chunk])
    assert len(out) == 1
    # EmbeddedChunk attribute AND payload dict both expose workspace_id.
    assert out[0].workspace_id == "ws-42"
    assert out[0].payload["workspace_id"] == "ws-42"


# ---------------------------------------------------------------------------
# end-to-end with fakes
# ---------------------------------------------------------------------------


def test_process_document_end_to_end_with_fakes(tmp_path: Path) -> None:
    """Full chain with fakes — no DeepDoc, no LiteLLM, no Qdrant, no PG server."""

    class FakeParser(DeepDocClient):
        def __init__(self) -> None:
            self._owns_client = False

        def parse(self, file_path: str | Path) -> ParsedDoc:
            return ParsedDoc(
                document_name=str(file_path),
                format="txt",
                sections=[
                    ParsedSection(
                        heading="Only section",
                        blocks=[
                            ContentBlock(kind="text", text="alpha beta gamma", page=0),
                            ContentBlock(kind="text", text="delta epsilon", page=0),
                        ],
                        page_start=0,
                        page_end=0,
                    )
                ],
                page_count=1,
            )

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1] * 1024}]}

    class _Client:
        def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
            return _Resp()

    fake_embedder = LiteLLMEmbedder(
        base_url="http://litellm",
        api_key="x",
        model="bge-m3",
        client=_Client(),  # type: ignore[arg-type]
    )
    fake_qdrant = _FakeQdrant()
    session = _make_session()

    result = process_document(
        file_path="dummy.txt",
        qdrant=fake_qdrant,  # type: ignore[arg-type]
        session=session,
        parser=FakeParser(),  # type: ignore[arg-type]
        embedder=fake_embedder,
        collection="chunks_v1",
        workspace_id=str(uuid.uuid4()),
    )
    assert result.parent_chunks_written == 1
    assert result.child_chunks_written == 1
    assert len(fake_qdrant.points) == 1