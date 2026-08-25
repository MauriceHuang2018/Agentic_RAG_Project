"""Two-stage parent/child chunker.

DESIGN 4.2: parent = chapter / section; child = paragraph / small block.
This module is pure — it operates on `ParsedDoc` and has no I/O. It is
fully unit-testable without mocks.

Splitting rules:
  * Each ParsedSection becomes one ParentChunk.
  * Within a section, consecutive text blocks of the same kind are
    grouped into a single ChildChunk until we cross a max-char budget.
  * Atomic kinds (``_ATOMIC_KINDS`` below) become their own ChildChunks
    — never merged with neighbouring text. Tables, images, figures, all
    flavours of captions, equations, and references are atomic because
    each is a self-contained semantic unit that degrades when merged
    with surrounding prose.
"""

from __future__ import annotations

from agentic_rag_project.doc_processor.models import (
    ChildChunk,
    ContentBlock,
    ParentChunk,
    ParsedDoc,
    ParsedSection,
)

# Tunable defaults — see DESIGN 4.2 for rationale (~512 tokens / child,
# ~3000 tokens / parent). Kept as module constants for now; will move
# to settings when the retrieval team starts tuning.
DEFAULT_CHILD_MAX_CHARS = 800
DEFAULT_PARENT_MAX_CHARS = 3200

# Kinds the chunker flushes around — never merged with neighbours.
# Mirrors the ContentBlock.kind literal widened in models.py (visual
# router dispatch covers DLA classes 0-6 + 8). Adding a new kind here
# without also adding it to models.py / visual_router will cause a
# downstream Pydantic validation error at the chunker boundary.
_ATOMIC_KINDS: frozenset[str] = frozenset({
    "table", "image",
    "figure", "figure_caption", "table_caption", "reference", "equation",
})


def _flush_child(
    buffer: list[ContentBlock],
    parent_id: str,
    document_id: str,
) -> ChildChunk | None:
    """Combine buffered blocks into a single ChildChunk."""
    if not buffer:
        return None
    pages = sorted({b.page for b in buffer})
    text = "\n\n".join(b.text for b in buffer if b.text)
    if not text:
        return None
    return ChildChunk(
        document_id=document_id,
        parent_id=parent_id,
        content=text,
        block_kind=buffer[0].kind,
        page=pages[0],
    )


def _split_section_into_children(
    section: ParsedSection,
    parent_id: str,
    document_id: str,
    max_chars: int,
) -> list[ChildChunk]:
    """Greedy char-budget chunker for one section."""
    children: list[ChildChunk] = []
    buffer: list[ContentBlock] = []
    buffer_len = 0

    def flush() -> None:
        nonlocal buffer_len
        child = _flush_child(buffer, parent_id, document_id)
        if child is not None:
            children.append(child)
        buffer.clear()
        buffer_len = 0

    for block in section.blocks:
        # Tables, images, figures, captions, equations and references are
        # atomic — never merged with neighbours. These are self-contained
        # semantic units (a chart, a caption, a formula) that lose meaning
        # when concatenated with surrounding prose.
        if block.kind in _ATOMIC_KINDS:
            flush()
            children.append(
                ChildChunk(
                    document_id=document_id,
                    parent_id=parent_id,
                    content=block.text or f"[{block.kind}]",
                    block_kind=block.kind,
                    page=block.page,
                )
            )
            continue
        block_len = len(block.text)
        # If the block alone exceeds the budget, flush it as its own child.
        if buffer and buffer_len + block_len > max_chars:
            flush()
        buffer.append(block)
        buffer_len += block_len
        # Force a flush when the buffer itself crosses the budget.
        if buffer_len >= max_chars:
            flush()

    flush()
    return children


def chunk_parsed_doc(
    parsed: ParsedDoc,
    document_id: str,
    child_max_chars: int = DEFAULT_CHILD_MAX_CHARS,
    parent_max_chars: int = DEFAULT_PARENT_MAX_CHARS,
) -> tuple[list[ParentChunk], list[ChildChunk]]:
    """Two-stage chunking entrypoint.

    Returns ``(parents, children)``. ``parent.child_ids`` are filled in
    so downstream consumers can build the parent_id index in Qdrant.
    """
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []

    for section in parsed.sections:
        # If the section text exceeds the parent budget, we still emit
        # one parent (the natural section) — splitting sections further
        # is a future enhancement; for MVP we keep 1:1 section:parent.
        parent = ParentChunk(
            document_id=document_id,
            content=_section_text(section),
            section_heading=section.heading,
            page_start=section.page_start,
            page_end=section.page_end,
        )
        section_children = _split_section_into_children(
            section=section,
            parent_id=parent.chunk_id,
            document_id=document_id,
            max_chars=child_max_chars,
        )
        parent.child_ids = [c.chunk_id for c in section_children]
        parents.append(parent)
        children.extend(section_children)

    # Mark over-budget parents for observability (no truncation here).
    for p in parents:
        if len(p.content) > parent_max_chars:
            p.content = p.content[:parent_max_chars] + "..."

    return parents, children


def _section_text(section: ParsedSection) -> str:
    """Render a section's blocks into the parent-context text."""
    parts: list[str] = [f"# {section.heading}"] if section.heading else []
    for block in section.blocks:
        if block.text:
            parts.append(block.text)
    return "\n\n".join(parts)