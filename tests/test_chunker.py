"""Chunker unit tests — atomic-set boundaries and basic grouping rules.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

The chunker never merges atomic kinds (table, image, figure, figure_caption,
table_caption, reference, equation) with neighbouring prose. These tests
guard the boundary contract so a future addition to ``_ATOMIC_KINDS`` —
or a typo'd kind string — fails loudly rather than silently merging
diagrams / captions / equations into surrounding paragraphs.
"""

from __future__ import annotations

from agentic_rag_project.doc_processor.chunker import (
    _ATOMIC_KINDS,
    chunk_parsed_doc,
)
from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)


def _block(kind: str, text: str, page: int = 0) -> ContentBlock:
    """Build a minimal ContentBlock for chunker tests."""
    return ContentBlock(kind=kind, text=text, page=page)


def test_atomic_set_includes_all_extended_kinds() -> None:
    """Atomic set must cover every kind the visual router can produce.

    Adding a new kind to ``ContentBlock.kind`` Literal without also
    adding it here (or deciding it should merge) would silently break
    the chunker's "never merge with neighbours" guarantee.
    """
    expected = {
        "table", "image",
        "figure", "figure_caption", "table_caption", "reference", "equation",
    }
    assert expected.issubset(_ATOMIC_KINDS)


def test_consecutive_text_blocks_merge_into_one_child() -> None:
    """Three short text blocks in the same section produce one child."""
    section = ParsedSection(
        heading="p1",
        level=1,
        blocks=[
            _block("text", "alpha"),
            _block("text", "beta"),
            _block("text", "gamma"),
        ],
        page_start=0,
        page_end=0,
    )
    parsed = ParsedDoc(
        document_name="x", format="pdf",
        sections=[section], page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1")
    assert len(children) == 1
    assert children[0].block_kind == "text"
    assert "alpha" in children[0].content
    assert "beta" in children[0].content
    assert "gamma" in children[0].content


def test_text_then_figure_caption_then_text_yields_three_children() -> None:
    """Atomic kinds flush the buffer — neighbours do not absorb them."""
    section = ParsedSection(
        heading="p1",
        level=1,
        blocks=[
            _block("text", "before"),
            _block("figure_caption", "Figure 1: revenue trend"),
            _block("text", "after"),
        ],
        page_start=0,
        page_end=0,
    )
    parsed = ParsedDoc(
        document_name="x", format="pdf",
        sections=[section], page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1")
    assert len(children) == 3
    assert [c.block_kind for c in children] == ["text", "figure_caption", "text"]
    # Atomic child's content is exactly the caption — not concatenated
    # with neighbours.
    assert children[1].content == "Figure 1: revenue trend"


def test_table_block_is_atomic_even_with_neighbouring_text() -> None:
    """Tables never merge with surrounding prose."""
    section = ParsedSection(
        heading="p1",
        level=1,
        blocks=[
            _block("text", "intro"),
            _block("table", "| a | b |\n|---|---|\n| 1 | 2 |"),
            _block("text", "post"),
        ],
        page_start=0,
        page_end=0,
    )
    parsed = ParsedDoc(
        document_name="x", format="pdf",
        sections=[section], page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1")
    assert len(children) == 3
    assert [c.block_kind for c in children] == ["text", "table", "text"]
    assert "1 | 2" in children[1].content
    # The table child does not leak surrounding prose into its content.
    assert "intro" not in children[1].content
    assert "post" not in children[1].content


def test_all_extended_atomic_kinds_flush_the_buffer() -> None:
    """Every kind in the atomic set is actually treated as atomic."""
    kinds_to_check = sorted(_ATOMIC_KINDS)
    section = ParsedSection(
        heading="p1",
        level=1,
        blocks=(
            [_block("text", "before")]
            + [_block(kind, f"atomic-{kind}") for kind in kinds_to_check]
            + [_block("text", "after")]
        ),
        page_start=0,
        page_end=0,
    )
    parsed = ParsedDoc(
        document_name="x", format="pdf",
        sections=[section], page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1")
    # 1 (before) + len(atomic kinds) + 1 (after) = 2 + N children
    assert len(children) == 2 + len(kinds_to_check)
    atomic_children = [c for c in children if c.block_kind != "text"]
    assert sorted(c.block_kind for c in atomic_children) == kinds_to_check


def test_atomic_block_with_empty_text_still_emits_placeholder_child() -> None:
    """An empty-text atomic block (e.g. figure placeholder) still produces
    a child so the chunker does not silently drop it."""
    section = ParsedSection(
        heading="p1",
        level=1,
        blocks=[
            _block("figure", "[figure @ page 1, bbox=(0,0,100,100)]"),
        ],
        page_start=0,
        page_end=0,
    )
    parsed = ParsedDoc(
        document_name="x", format="pdf",
        sections=[section], page_count=1,
    )
    _, children = chunk_parsed_doc(parsed, document_id="doc-1")
    assert len(children) == 1
    assert children[0].block_kind == "figure"
    assert "bbox=" in children[0].content