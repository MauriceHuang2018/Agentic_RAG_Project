"""Document parser — format dispatcher + DeepDoc visual router.

DESIGN 4.2 (legacy): "OCR strategy: RAGFlow DeepDoc built-in + RapidOCR fallback".
DESIGN parser_router/DESIGN_parser_router.md (current):
    structured formats (PDF text / DOCX / PPTX / XLSX / MD / TXT) are parsed
    in-process by pip libraries; DeepDoc Server is only consulted for scanned
    PDFs and standalone images via `visual_router.DeepDocVisualRouter`.

The legacy `DeepDocClient.parse()` endpoint `/v1/document/parse` is kept as a
backward-compatible shim — it now degrades to RapidOCR-only since RAGFlow's
real full-stack API does not expose that path. New callers should use
`parse_document_with_router()` or the in-process extractors.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from agentic_rag_project.config import get_settings
from agentic_rag_project.doc_processor.models import (
    BoundingBox,
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)
from agentic_rag_project.doc_processor.text_extractors import (
    extract_docx,
    extract_pdf_text,
    extract_plain,
    extract_pptx,
    extract_xlsx,
    infer_format,
    is_scanned_pdf,
)

# Backward-compatible alias — `tests/test_doc_processor.py` imports the
# private `_infer_format` symbol; keep that name available so older tests
# do not break when the parser is refactored to delegate to text_extractors.
_infer_format = infer_format

logger = logging.getLogger(__name__)

# DeepDoc uses multipart upload; auth via Authorization header (legacy only —
# DeepDoc Server itself is unauthenticated).
_DEEPDOC_TIMEOUT_SECONDS = 120.0


class ParserError(Exception):
    """Raised when both DeepDoc and the RapidOCR fallback fail."""


def _coerce_block(raw: dict[str, Any]) -> ContentBlock:
    """Convert a raw DeepDoc block dict into our ContentBlock model."""
    bbox_raw = raw.get("bbox")
    bbox = None
    if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4:
        bbox = BoundingBox(x0=bbox_raw[0], y0=bbox_raw[1], x1=bbox_raw[2], y1=bbox_raw[3])
    return ContentBlock(
        kind=raw.get("type", "text"),
        text=raw.get("text", "") or "",
        level=int(raw.get("level", 0) or 0),
        page=int(raw.get("page", 0) or 0),
        bbox=bbox,
    )


def _build_parsed_doc(name: str, fmt: str, payload: dict[str, Any]) -> ParsedDoc:
    """Translate DeepDoc JSON into ParsedDoc; tolerant of schema drift."""
    sections_raw = payload.get("sections", []) or []
    sections: list[ParsedSection] = []
    for sec_raw in sections_raw:
        blocks_raw = sec_raw.get("blocks", []) or []
        blocks = [_coerce_block(b) for b in blocks_raw if isinstance(b, dict)]
        if not blocks:
            continue
        pages = sorted({b.page for b in blocks})
        page_start = pages[0] if pages else 0
        page_end = pages[-1] if pages else 0
        sections.append(
            ParsedSection(
                heading=sec_raw.get("heading", "") or "(untitled)",
                level=int(sec_raw.get("level", 1) or 1),
                blocks=blocks,
                page_start=page_start,
                page_end=page_end,
            )
        )
    return ParsedDoc(
        document_name=name,
        format=fmt,  # type: ignore[arg-type]
        sections=sections,
        page_count=int(payload.get("page_count", 0) or 0),
    )


def _rapidocr_fallback(path: Path, fmt: str) -> ParsedDoc:
    """RapidOCR for image-only PDFs or when DeepDoc returns nothing."""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as exc:
        raise ParserError("rapidocr-onnxruntime is not installed") from exc
    engine = RapidOCR()
    result, _ = engine(str(path))
    text_lines = [line[1] for line in (result or []) if len(line) >= 2]
    if not text_lines:
        raise ParserError(f"RapidOCR produced no text for {path}")
    section = ParsedSection(
        heading="(OCR fallback)",
        level=1,
        blocks=[ContentBlock(kind="text", text="\n".join(text_lines), page=0)],
        page_start=0,
        page_end=0,
    )
    return ParsedDoc(
        document_name=path.name,
        format=fmt,  # type: ignore[arg-type]
        sections=[section],
        ocr_used=True,
        page_count=1,
    )


class DeepDocClient:
    """HTTP wrapper around RAGFlow DeepDoc Server (LitServe, port 9390).

    Two surfaces:
      - Legacy `parse(file_path)` calls `/v1/document/parse`, which is
        NOT exposed by the standalone DeepDoc Server; it now degrades to
        RapidOCR and logs a warning. Kept for backward compatibility only.
      - New `_predict_dla / _predict_ocr / _predict_tsr / health` methods
        target the real `/predict/*` endpoints consumed by
        `visual_router.DeepDocVisualRouter`.

    DeepDoc Server is unauthenticated (stateless inference service assumed
    to run on a trusted internal network), so the `api_key` argument is
    retained for API stability but never sent on the wire.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.ragflow_base_url).rstrip("/")
        self._api_key = api_key or settings.ragflow_api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=_DEEPDOC_TIMEOUT_SECONDS)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def health(self) -> bool:
        """Liveness probe for the DeepDoc Server."""
        try:
            response = self._client.get(f"{self._base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def predict_dla(self, image_bytes: bytes, *, filename: str = "page.jpg") -> dict[str, Any]:
        """Document Layout Analysis — returns layout boxes for one page image."""
        return self._post_image("/predict/dla", image_bytes, filename=filename)

    def predict_ocr(
        self,
        image_bytes: bytes,
        *,
        operator: str = "rec",
        filename: str = "page.jpg",
    ) -> dict[str, Any]:
        """OCR — `operator="rec"` for recognition, "det" for detection only."""
        return self._post_image(
            "/predict/ocr",
            image_bytes,
            filename=filename,
            data={"operator": operator},
        )

    def predict_tsr(self, image_bytes: bytes, *, filename: str = "page.jpg") -> dict[str, Any]:
        """Table Structure Recognition — returns HTML / cells for one cropped table."""
        return self._post_image("/predict/tsr", image_bytes, filename=filename)

    def _post_image(
        self,
        path: str,
        image_bytes: bytes,
        *,
        filename: str,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send a JPEG image to a DeepDoc Server `/predict/*` endpoint."""
        files = {"request": (filename, image_bytes, "image/jpeg")}
        response = self._client.post(
            f"{self._base_url}{path}",
            files=files,
            data=data or {},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ParserError(f"deepdoc {path} returned non-dict payload")
        return payload

    def parse(self, file_path: str | Path) -> ParsedDoc:
        """Legacy single-shot parse — degrades to RapidOCR.

        Kept so existing callers (`DocProcessor`, `parse_document`) still
        work; new callers should use `parse_document_with_router`.
        """
        path = Path(file_path)
        fmt = infer_format(path)
        logger.warning(
            "DeepDocClient.parse() is deprecated for the new standalone DeepDoc "
            "Server — it has no /v1/document/parse endpoint. Falling back to RapidOCR."
        )
        return _rapidocr_fallback(path, fmt)

    def _fallback_ocr(self, path: Path, fmt: str) -> ParsedDoc:
        """Backward-compatible alias — see `_rapidocr_fallback`."""
        return _rapidocr_fallback(path, fmt)

    def close(self) -> None:
        """Release the underlying HTTP client if we own it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DeepDocClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def parse_document(file_path: str | Path, client: DeepDocClient | None = None) -> ParsedDoc:
    """Legacy single-shot parse — see `parse_document_with_router` for the new API."""
    if client is None:
        with DeepDocClient() as c:
            return c.parse(file_path)
    return client.parse(file_path)


# ---- format dispatcher (parser_router entrypoint) --------------------------


def parse_document_with_router(
    file_path: str | Path,
    *,
    visual_router: "DeepDocVisualRouter | None" = None,
    avg_chars_threshold: int | None = None,
) -> ParsedDoc:
    """Top-level format dispatcher used by Celery workers and the upload API.

    Routes by extension:
      - PDF: `is_scanned_pdf()` heuristic decides between PyMuPDF text and
        `visual_router`. Falls back to PyMuPDF when no router is supplied.
      - DOCX / PPTX / XLSX / MD / TXT: in-process pip extractors, never DeepDoc.

    Args:
        file_path: Path to the uploaded document.
        visual_router: DeepDocVisualRouter instance (or None for text-only PDFs).
        avg_chars_threshold: Override the scan heuristic threshold.

    Returns:
        ParsedDoc ready for chunking.

    Raises:
        ValueError: when the file extension is not supported.
        FileNotFoundError: when `file_path` does not exist.
    """
    path = Path(file_path)
    fmt = infer_format(path)
    if fmt == "pdf":
        return _parse_pdf(path, visual_router, avg_chars_threshold)
    if fmt == "image":
        return _parse_image(path, visual_router)
    if fmt == "docx":
        return extract_docx(path)
    if fmt == "pptx":
        return extract_pptx(path)
    if fmt == "xlsx":
        return extract_xlsx(path)
    if fmt in {"md", "txt"}:
        return extract_plain(path)
    # infer_format raises so this branch is unreachable; defensive anyway.
    raise ValueError(f"unhandled format: {fmt}")


def _parse_image(
    path: Path,
    visual_router: "DeepDocVisualRouter | None",
) -> ParsedDoc:
    """Route a raw image (jpg/png/...) to the DeepDoc visual router.

    Images have no embedded text layer, so the in-process PyMuPDF /
    python-docx / python-pptx / pandas extractors cannot help us here.
    A ``visual_router`` is therefore required — without one we cannot
    honour the design's "structured formats use pip libs, everything
    else goes through DeepDoc" contract.
    """
    if visual_router is None:
        raise ValueError(
            f"image format {path.suffix!r} requires a DeepDoc visual_router; "
            "no in-process extractor is available for raw images"
        )
    return visual_router.parse_images([path.read_bytes()], document_name=path.name)


def _parse_pdf(
    path: Path,
    visual_router: "DeepDocVisualRouter | None",
    avg_chars_threshold: int | None,
) -> ParsedDoc:
    """Decide between PyMuPDF text extraction and DeepDoc visual routing."""
    settings = get_settings()
    threshold = (
        avg_chars_threshold
        if avg_chars_threshold is not None
        else settings.pdf_scanned_avg_chars_threshold
    )
    if visual_router is not None and is_scanned_pdf(path, avg_chars_threshold=threshold):
        logger.info("PDF detected as scanned/complex — routing to DeepDoc visual path")
        return visual_router.parse_pdf(path)
    return extract_pdf_text(path, avg_chars_threshold=threshold)


# Imported lazily to avoid circular import (visual_router imports parser too).
def _get_visual_router_class():
    from agentic_rag_project.doc_processor.visual_router import DeepDocVisualRouter
    return DeepDocVisualRouter
