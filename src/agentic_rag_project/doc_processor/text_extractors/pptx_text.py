"""PPTX extraction via python-pptx — one section per slide.

DESIGN: parser_router/DESIGN_parser_router.md
- Each slide becomes one `ParsedSection` so page boundaries are preserved.
- The slide title is used as the section heading when present; otherwise we
  fall back to "Slide N" so the chunker never gets an empty heading.
- All text shapes inside a slide are concatenated into a single text block —
  PPTX lacks structural hierarchy between shapes so per-shape blocks would
  fragment the chunker output unnecessarily.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation  # type: ignore[import-not-found]

from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)


def extract_pptx(file_path: str | Path) -> ParsedDoc:
    """Extract a PPTX into a `ParsedDoc` with one section per slide.

    Raises:
        FileNotFoundError: when `file_path` does not exist.
        PackageNotFoundError: when the file is not a valid PPTX zip.
    """
    path = Path(file_path)
    presentation = Presentation(str(path))
    sections: list[ParsedSection] = []
    for slide_idx, slide in enumerate(presentation.slides):
        title = ""
        text_parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if not text:
                        continue
                    if not title and shape == slide.shapes.title:
                        title = text
                    else:
                        text_parts.append(text)
        heading = title or f"Slide {slide_idx + 1}"
        blocks: list[ContentBlock] = []
        if text_parts:
            blocks.append(
                ContentBlock(
                    kind="text",
                    text="\n".join(text_parts),
                    page=slide_idx,
                )
            )
        sections.append(
            ParsedSection(
                heading=heading,
                level=1,
                blocks=blocks,
                page_start=slide_idx,
                page_end=slide_idx,
            )
        )
    return ParsedDoc(
        document_name=path.name,
        format="pptx",
        sections=sections,
        page_count=len(presentation.slides),
    )
