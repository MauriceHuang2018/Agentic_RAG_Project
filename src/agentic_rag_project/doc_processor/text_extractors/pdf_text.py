"""PDF text extraction via PyMuPDF + scan heuristic.

DESIGN: parser_router/DESIGN_parser_router.md
- Pure text layer: PyMuPDF `Page.get_text()` preserves the embedded text layer.
- Each page becomes one `ParsedSection` so the chunker can keep page boundaries.
- `is_scanned_pdf` is the scan-detection heuristic used by the router to decide
  whether to dispatch to DeepDoc visual_router.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf  # type: ignore[import-not-found]

from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)


def extract_pdf_text(
    file_path: str | Path,
    *,
    avg_chars_threshold: int = 100,
) -> ParsedDoc:
    """Extract text from a PDF with an embedded text layer using PyMuPDF.

    Args:
        file_path: Path to the PDF on disk.
        avg_chars_threshold: Heuristic threshold for the scan check (unused here,
            kept for parity with `is_scanned_pdf` so callers pass it through).

    Returns:
        ParsedDoc with one section per page.

    Raises:
        FileNotFoundError: when `file_path` does not exist.
        pymupdf.FileDataError: when the file is not a valid PDF.
    """
    path = Path(file_path)
    doc = pymupdf.open(str(path))
    try:
        sections: list[ParsedSection] = []
        for page_idx, page in enumerate(doc):
            text = page.get_text("text") or ""
            blocks = [
                ContentBlock(kind="text", text=text, page=page_idx)
            ] if text.strip() else []
            if blocks:
                sections.append(
                    ParsedSection(
                        heading=f"Page {page_idx + 1}",
                        level=1,
                        blocks=blocks,
                        page_start=page_idx,
                        page_end=page_idx,
                    )
                )
        return ParsedDoc(
            document_name=path.name,
            format="pdf",
            sections=sections,
            page_count=len(doc),
        )
    finally:
        doc.close()


def is_scanned_pdf(
    file_path: str | Path,
    *,
    avg_chars_threshold: int = 100,
    avg_images_threshold: float = 0.5,
) -> bool:
    """Heuristic check: low text density + many images => scanned/complex PDF.

    True means the caller should route the file to the DeepDoc visual path
    instead of using the embedded text layer.
    """
    path = Path(file_path)
    doc = pymupdf.open(str(path))
    try:
        if len(doc) == 0:
            return False
        total_chars = 0
        total_images = 0
        for page in doc:
            total_chars += len(page.get_text("text") or "")
            total_images += len(page.get_images(full=True))
        avg_chars = total_chars / len(doc)
        avg_images = total_images / len(doc)
        return avg_chars < avg_chars_threshold or avg_images > avg_images_threshold
    finally:
        doc.close()


def render_pdf_pages_to_images(
    file_path: str | Path,
    *,
    dpi: int = 150,
) -> list[bytes]:
    """Render every page of a PDF to JPEG bytes for the DeepDoc visual path.

    Returns one JPEG per page in document order. Bytes (not PIL.Image) so the
    caller can stream them straight to DeepDoc's `/predict/*` multipart
    endpoint without intermediate disk writes.
    """
    path = Path(file_path)
    doc = pymupdf.open(str(path))
    try:
        images: list[bytes] = []
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(pix.tobytes("jpeg"))
        return images
    finally:
        doc.close()
