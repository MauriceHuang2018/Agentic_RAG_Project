"""Format-specific text extractors — bypass RAGFlow DeepDoc for structured formats.

DESIGN: parser_router/DESIGN_parser_router.md (commit F2)
Each `extract_<format>(path) -> ParsedDoc` is a pure function so unit tests
can mock without booting any service.
"""

from __future__ import annotations

from pathlib import Path

from agentic_rag_project.doc_processor.text_extractors.docx_text import extract_docx
from agentic_rag_project.doc_processor.text_extractors.pdf_text import (
    extract_pdf_text,
    is_scanned_pdf,
)
from agentic_rag_project.doc_processor.text_extractors.pptx_text import extract_pptx
from agentic_rag_project.doc_processor.text_extractors.plain_text import extract_plain
from agentic_rag_project.doc_processor.text_extractors.xlsx_text import extract_xlsx

__all__ = [
    "extract_docx",
    "extract_pdf_text",
    "extract_pptx",
    "extract_plain",
    "extract_xlsx",
    "is_scanned_pdf",
]


def infer_format(path: str | Path) -> str:
    """Map file extension to a parser_router format token.

    Returns:
        ``"pdf"``, ``"docx"``, ``"pptx"``, ``"xlsx"``, ``"md"``, ``"txt"``,
        or ``"image"`` for raw image extensions.

    Raises:
        ValueError: when the extension is not supported by the dispatcher.
    """
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"pdf", "docx", "pptx", "xlsx", "md", "txt"}:
        return suffix
    # Raw images are routed as a single format token so the dispatcher can
    # hand them to the DeepDoc visual router without per-extension branching.
    if suffix in {"jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp"}:
        return "image"
    raise ValueError(f"unsupported file extension for parser_router: .{suffix}")
