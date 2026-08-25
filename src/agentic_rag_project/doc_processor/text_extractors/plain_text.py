"""Plain-text extraction for Markdown (.md) and plain text (.txt).

DESIGN: parser_router/DESIGN_parser_router.md
- Markdown: split into sections by `#` / `##` / `###` headings so the chunker
  can preserve heading hierarchy. Inline `#` mid-line is NOT treated as a
  heading (we only match `#` at the start of a line).
- Plain text: one section per ~30 lines (same density used by the DOCX
  heuristic) — we deliberately keep the chunker output shape consistent
  across all formats.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_LINES_PER_SECTION = 30


def extract_plain(file_path: str | Path) -> ParsedDoc:
    """Extract a Markdown or plain-text file into a `ParsedDoc`.

    Markdown is detected by extension: `.md` uses heading-based sections,
    everything else (including `.txt`) is chunked by fixed line windows.

    Raises:
        FileNotFoundError: when `file_path` does not exist.
        UnicodeDecodeError: when the file is not UTF-8 decodable.
    """
    path = Path(file_path)
    text = path.read_text(encoding="utf-8")
    fmt = "md" if path.suffix.lower() == ".md" else "txt"
    if fmt == "md":
        sections = _split_markdown(text)
    else:
        sections = _split_plain(text)
    page_count = max(1, (len(text.splitlines()) + _LINES_PER_SECTION - 1) // _LINES_PER_SECTION)
    return ParsedDoc(
        document_name=path.name,
        format=fmt,
        sections=sections,
        page_count=page_count,
    )


def _split_markdown(text: str) -> list[ParsedSection]:
    """Walk lines, opening a new section on every `# ..###### ` heading."""
    sections: list[ParsedSection] = []
    current: ParsedSection | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current is not None:
                _flush_buffer(current, buffer)
            current = ParsedSection(
                heading=match.group(2).strip(),
                level=len(match.group(1)),
            )
            sections.append(current)
            buffer = []
        else:
            buffer.append(line)
    if current is None:
        # No headings found — treat the whole file as one section.
        current = ParsedSection(heading="(document)", level=1)
        sections.append(current)
    _flush_buffer(current, buffer)
    return sections


def _split_plain(text: str) -> list[ParsedSection]:
    """Chunk plain text by fixed line windows so the chunker sees uniform input."""
    lines = text.splitlines()
    sections: list[ParsedSection] = []
    for chunk_idx in range(0, len(lines), _LINES_PER_SECTION):
        chunk = lines[chunk_idx: chunk_idx + _LINES_PER_SECTION]
        body = "\n".join(line for line in chunk if line.strip())
        if not body:
            continue
        sections.append(
            ParsedSection(
                heading=f"(lines {chunk_idx + 1}-{chunk_idx + len(chunk)})",
                level=1,
                blocks=[ContentBlock(kind="text", text=body, page=chunk_idx // _LINES_PER_SECTION)],
                page_start=chunk_idx // _LINES_PER_SECTION,
                page_end=chunk_idx // _LINES_PER_SECTION,
            )
        )
    if not sections:
        sections.append(
            ParsedSection(
                heading="(empty)",
                level=1,
                blocks=[],
                page_start=0,
                page_end=0,
            )
        )
    return sections


def _flush_buffer(section: ParsedSection, buffer: list[str]) -> None:
    """Convert accumulated body lines into a single text block on `section`."""
    body = "\n".join(line for line in buffer if line.strip())
    if body:
        section.blocks.append(ContentBlock(kind="text", text=body, page=0))
