"""DOCX extraction via python-docx, structured by heading hierarchy.

DESIGN: parser_router/DESIGN_parser_router.md
- Each top-level heading (`Heading 1`) opens a new `ParsedSection`.
- Sub-headings (`Heading 2..6`) become `ContentBlock(kind="heading", level=N)`
  within the current section, preserving hierarchy for the chunker.
- Lists are detected by the `numbering` attribute on a paragraph and become
  `ContentBlock(kind="list", text=...)` blocks.
- Inline images are intentionally skipped — structured text only (Q1 decision:
  complex image-heavy DOCX is out of scope for this iteration).
"""

from __future__ import annotations

from pathlib import Path

from docx import Document  # type: ignore[import-not-found]

from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)


def _detect_level(paragraph) -> int:
    """Map a python-docx paragraph to a heading level (0 = body)."""
    style_name = (paragraph.style.name or "").strip().lower()
    if style_name.startswith("heading"):
        try:
            return int(style_name.split()[-1])
        except (ValueError, IndexError):
            return 1
    return 0


def _is_list_item(paragraph) -> bool:
    """Detect list bullets (Word numbering applied to the paragraph)."""
    return bool(getattr(paragraph, "_p", None) is not None
                and paragraph._p.xpath(".//w:numPr"))


def extract_docx(file_path: str | Path) -> ParsedDoc:
    """Extract a DOCX into a `ParsedDoc` grouped by top-level headings.

    Sections split on `Heading 1`. Sub-headings become in-section blocks
    so the chunker can preserve heading hierarchy.

    Raises:
        FileNotFoundError: when `file_path` does not exist.
        PackageNotFoundError: when the file is not a valid DOCX zip.
    """
    path = Path(file_path)
    document = Document(str(path))
    sections: list[ParsedSection] = []
    current: ParsedSection | None = None

    for paragraph in document.paragraphs:
        level = _detect_level(paragraph)
        text = paragraph.text or ""
        if level == 1 and text.strip():
            current = ParsedSection(heading=text.strip(), level=1)
            sections.append(current)
            continue
        if current is None:
            # Body before the first heading — bucket into a synthetic section
            current = ParsedSection(heading="(preamble)", level=1)
            sections.append(current)
        kind = "list" if _is_list_item(paragraph) else "text"
        if level >= 2:
            current.blocks.append(
                ContentBlock(kind="heading", text=text, level=level, page=0)
            )
        elif text.strip():
            current.blocks.append(
                ContentBlock(kind=kind, text=text, level=0, page=0)
            )

    # Estimate page count from paragraph density (DOCX has no real page concept)
    page_count = max(1, len(document.paragraphs) // 30)
    return ParsedDoc(
        document_name=path.name,
        format="docx",
        sections=sections,
        page_count=page_count,
    )
