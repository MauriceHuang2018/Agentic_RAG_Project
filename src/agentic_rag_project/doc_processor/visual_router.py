"""DeepDoc visual router — orchestrates DLA + OCR + TSR for scanned PDFs / images.

DESIGN parser_router/DESIGN_parser_router.md
Pipeline:
    PDF / images
        |
        v
    render pages to JPEG (PyMuPDF for PDFs, pass-through for raw images)
        |
        v
    POST /predict/dla per page  →  layout boxes (text / table / figure)
        |
        +-- text box   → POST /predict/ocr?operator=rec
        +-- table box  → POST /predict/tsr
        |
        v
    aggregate into a ParsedDoc (one section per page)

Failure policy:
    - Any HTTP / parse failure short-circuits to RapidOCR for the whole batch,
      preserving the legacy "DeepDoc when available, OCR otherwise" contract.
"""

from __future__ import annotations

import io
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import httpx
from PIL import Image  # type: ignore[import-not-found]

from agentic_rag_project.doc_processor.models import (
    BoundingBox,
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)
from agentic_rag_project.doc_processor.text_extractors.pdf_text import (
    render_pdf_pages_to_images,
)

logger = logging.getLogger(__name__)


# DeepDoc DLA ``class_id`` conventions — see infiniflow/ragflow deepdoc
# README and the live LitServe server at port 9390 (verified by
# ``diag_tsr.py`` run on 2026-08-24, 29-page research-report PDF).
#
#   0 = title,        1 = text,             2 = reference,
#   3 = figure,       4 = figure_caption,   5 = table,
#   6 = table_caption, 8 = equation
#
# Routing per class_id:
#   * 0/1/2 → OCR-rec text + RapidOCR per-crop fallback (``kind="text"``).
#     Reference lists (2) used to be dropped, which silently lost the
#     whole bibliography of any paper — now treated as text.
#   * 3    → figure placeholder (no OCR); bbox-only ``kind="figure"``.
#   * 4/6/8 → caption / equation via OCR-rec + RapidOCR fallback; the
#     three are atomic in the chunker because each is a self-contained
#     semantic unit.
#   * 5    → TSR with per-cell OCR cascade; ``kind="table"``.
_DLA_CLASS_TEXT_LIKE: frozenset[float] = frozenset({0.0, 1.0, 2.0})
_DLA_CLASS_TABLE: frozenset[float] = frozenset({5.0})

# Hard ceiling on cells we will OCR per table — protects against
# pathological inputs (e.g. p28 box2 = 5×46 = 230 cells, would take
# ~46s at 200ms/cell). When exceeded we emit the structural placeholder
# instead of attempting the cascade.
MAX_PER_CELL_OCR_CELLS: int = 200

# RapidOCR returns one string per detected span (often a single word). We
# collapse intra-span whitespace runs and join spans with a single space
# so multi-line paragraphs read as a single sentence — chunkers and
# embedding models (BGE-M3) both tokenize on whitespace, so "word1 word2"
# and "word1\nword2" embed identically, but the single-space form is far
# easier to inspect in logs and survives the chunker's `len(text)` budget
# count without inflating it with newline padding.
_WHITESPACE_RUN = re.compile(r"\s+")


class DeepDocVisualRouter:
    """Calls DeepDoc Server `/predict/{dla,ocr,tsr}` and aggregates to ParsedDoc.

    The `client` argument accepts a `DeepDocClient` so unit tests can inject
    a fake without any HTTP traffic. `dpi` controls PDF→JPEG rendering.
    """

    def __init__(
        self,
        client,
        *,
        dpi: int = 150,
        fallback_ocr: bool = True,
    ) -> None:
        self._client = client
        self._dpi = dpi
        self._fallback_ocr = fallback_ocr

    # ---- public API --------------------------------------------------------

    def parse_pdf(self, file_path: str | Path) -> ParsedDoc:
        """Render every page of a PDF to JPEG and run them through DLA/OCR/TSR."""
        path = Path(file_path)
        images = render_pdf_pages_to_images(path, dpi=self._dpi)
        return self.parse_images(images, document_name=path.name)

    def parse_images(
        self,
        images: Iterable[bytes],
        *,
        document_name: str = "(image)",
    ) -> ParsedDoc:
        """Aggregate JPEG bytes (one per page) into a ParsedDoc."""
        try:
            sections: list[ParsedSection] = []
            for page_idx, image_bytes in enumerate(images):
                layout = self._client.predict_dla(image_bytes)
                section = self._build_section_for_page(layout, image_bytes, page_idx)
                if section is not None:
                    sections.append(section)
            if not sections:
                raise RuntimeError("deepdoc returned no usable sections")
            return ParsedDoc(
                document_name=document_name,
                format="pdf",
                sections=sections,
                page_count=len(sections),
                ocr_used=True,  # every box went through DLA + OCR-rec / TSR
            )
        except (httpx.HTTPError, RuntimeError, OSError) as exc:
            if not self._fallback_ocr:
                raise
            logger.warning("deepdoc visual path failed: %s — falling back to RapidOCR", exc)
            return self._rapidocr_fallback(images, document_name=document_name)

    # ---- internals ---------------------------------------------------------

    def _build_section_for_page(
        self,
        layout: dict[str, Any],
        image_bytes: bytes,
        page_idx: int,
    ) -> ParsedSection | None:
        """Translate one page's DLA output into a ParsedSection with content blocks.

        DeepDoc's DLA endpoint returns layout boxes under the ``bboxes`` key
        (verified against the real LitServe server).  Older / mock fixtures
        use ``boxes``.  Accept either so unit tests and live traffic agree.
        """
        boxes = layout.get("boxes") or layout.get("bboxes") or []
        blocks: list[ContentBlock] = []
        for box in boxes:
            block = self._box_to_block(box, image_bytes, page_idx)
            if block is not None:
                blocks.append(block)
        if not blocks:
            return None
        return ParsedSection(
            heading=f"Page {page_idx + 1}",
            level=1,
            blocks=blocks,
            page_start=page_idx,
            page_end=page_idx,
        )

    def _box_to_block(
        self,
        box: Any,
        image_bytes: bytes,
        page_idx: int,
    ) -> ContentBlock | None:
        """Route one DLA box by class_id and wrap the result into a block.

        Two box shapes are accepted:

        * **Real DeepDoc DLA list** (verified against the LitServe server):
          ``[x0, y0, x1, y1, score, class_id]``.  ``class_id`` follows the
          DeepDoc convention — see the routing table at module top.
        * **Mock / dict** (used by ``tests/test_parser_router.py``):
          ``{"type": "text"|"table", "bbox": [...]}``.

        Returns ``None`` for unknown class_ids (logged once at WARNING).
        """
        bbox, class_id = self._normalize_box(box)
        if bbox is None:
            return None

        # ---- class-5: table (TSR + per-cell OCR cascade) -----------------
        if class_id in _DLA_CLASS_TABLE:
            cropped = self._crop(image_bytes, bbox)
            payload = self._client.predict_tsr(cropped)
            text = self._format_tsr_as_markdown(
                payload,
                original_image=image_bytes,
                table_bbox=bbox,
                client=self._client,
            )
            return self._wrap_text_block(
                kind="table", text=text, page_idx=page_idx, bbox=bbox,
            )

        # ---- class-3: figure (no OCR — bbox placeholder only) -----------
        if class_id == 3.0:
            return ContentBlock(
                kind="figure",
                text=(
                    f"[figure @ page {page_idx + 1}, "
                    f"bbox=({bbox.x0:.0f},{bbox.y0:.0f},{bbox.x1:.0f},{bbox.y1:.0f})]"
                ),
                page=page_idx,
                bbox=bbox,
                meta={"dla_bbox": bbox.model_dump()},
            )

        # ---- class-4 / class-6 / class-8: caption / equation (atomic) ---
        if class_id == 4.0:
            kind = "figure_caption"
        elif class_id == 6.0:
            kind = "table_caption"
        elif class_id == 8.0:
            kind = "equation"
        elif class_id in _DLA_CLASS_TEXT_LIKE:
            kind = "text"
        else:
            # Defensive — unknown class_id (e.g. 7, future additions). Don't
            # silently drop without a trace; observability over silence.
            logger.warning("unknown DLA class_id %s — dropping box", class_id)
            return None

        text = self._ocr_text_with_fallback(image_bytes, bbox)
        return self._wrap_text_block(
            kind=kind, text=text, page_idx=page_idx, bbox=bbox,
        )

    def _wrap_text_block(
        self,
        *,
        kind: str,
        text: str,
        page_idx: int,
        bbox: BoundingBox,
    ) -> ContentBlock | None:
        """Trim empty text and wrap into a ``ContentBlock``.

        Shared exit point for every class path that produces text (text /
        caption / equation / table). Returns ``None`` when nothing usable
        came back so the caller doesn't have to re-check.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        return ContentBlock(
            kind=kind, text=cleaned, page=page_idx, bbox=bbox,
        )

    def _ocr_text_with_fallback(
        self,
        image_bytes: bytes,
        bbox: BoundingBox,
    ) -> str:
        """OCR a crop and fall back to RapidOCR when DeepDoc returns empty.

        Used by every non-table text-producing class (0/1/2/4/6/8). Kept as
        its own helper so ``_box_to_block`` stays a flat dispatch and the
        OCR-rec → RapidOCR cascade is testable in isolation.
        """
        cropped = self._crop(image_bytes, bbox)
        payload = self._client.predict_ocr(cropped, operator="rec")
        text = self._extract_ocr_text(payload)
        # OCR-rec returns empty on tall multi-line crops (verified
        # against scanned_pdf.pdf — boxes [1]/[2]/[3] at height ~125px
        # all returned ''). Fall back to RapidOCR on the same crop to
        # recover the paragraph; it handles multi-line layouts natively.
        if not text.strip():
            text = self._rapidocr_on_crop(cropped)
        return text

    def _normalize_box(self, box: Any) -> tuple[BoundingBox | None, float]:
        """Resolve a DLA box to ``(bbox, class_id)`` regardless of wire format.

        Real DeepDoc returns ``[x0, y0, x1, y1, score, class_id]``; legacy
        fixtures / mocks use ``{"type": "text"|"table", "bbox": [...]}``.
        """
        if isinstance(box, (list, tuple)):
            if len(box) < 4:
                return None, 0.0
            bbox = BoundingBox(
                x0=float(box[0]), y0=float(box[1]),
                x1=float(box[2]), y1=float(box[3]),
            )
            class_id = float(box[5]) if len(box) >= 6 else 1.0  # default = text
            return bbox, class_id
        # Dict shape — used by mock fixtures. Map ``type`` back to class_id
        # so downstream routing stays consistent with real DeepDoc outputs.
        bbox = self._coerce_bbox(box.get("bbox"))
        type_str = (box.get("type") or "text").lower()
        class_id = 5.0 if type_str == "table" else 1.0
        return bbox, class_id

    @staticmethod
    def _extract_ocr_text(payload: dict[str, Any]) -> str:
        """Flatten the OCR-rec 4-level nested payload into a single string.

        DeepDoc OCR-rec returns ``{"output": [[[ [text, score], ... ]]]}`` —
        ``output[0][0][0]`` is the items list and each item is a
        ``[text, score]`` pair (verified against the live LitServe server).
        """
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        try:
            items = output[0][0][0]
        except (IndexError, TypeError):
            return ""
        if not isinstance(items, list):
            return ""
        lines: list[str] = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                text = str(item[0]).strip()
                if text:
                    lines.append(text)
            elif isinstance(item, str):
                text = item.strip()
                if text:
                    lines.append(text)
        return "\n".join(lines)

    def _format_tsr_as_markdown(
        self,
        payload: dict[str, Any],
        *,
        original_image: bytes,
        table_bbox: BoundingBox,
        client: Any,
    ) -> str:
        """Render a TSR payload as a markdown table.

        Dispatcher with three stages:

        1. **HTML fast-path** (``_format_tsr_as_html``) — historically some
           DeepDoc versions return a serialized ``<table>``; the live
           server we target returns it as empty, so this stage is a
           hook for the future rather than live functionality today
           (verified 2026-08-24, 18/18 tables had no ``html`` field).
        2. **Per-cell OCR** (``_format_tsr_via_per_cell_ocr``) — crops each
           cell on the *original* page image (not the table crop, so
           coordinate offsets stay correct), runs OCR-rec, falls back to
           RapidOCR per-cell when OCR-rec is empty, and assembles a
           markdown table.
        3. **Placeholder** — emitted when (a) ``html`` was empty, (b)
           per-cell OCR exceeded ``MAX_PER_CELL_OCR_CELLS``, or (c)
           per-cell OCR produced nothing usable. Preserves the legacy
           ``| DeepDoc table | N cols × M rows |`` shape so existing
           consumers and the chunker don't break.
        """
        text = self._format_tsr_as_html(payload)
        if text is not None:
            return text

        text = self._format_tsr_via_per_cell_ocr(
            payload,
            original_image=original_image,
            table_bbox=table_bbox,
            client=client,
        )
        if text:
            return text

        return self._placeholder_table_text(payload)

    @staticmethod
    def _format_tsr_as_html(payload: dict[str, Any]) -> str | None:
        """Render the ``html`` field of a TSR payload to markdown.

        Returns ``None`` when the field is absent or empty so the caller
        falls back to per-cell OCR. The live LitServe server does not
        populate this field for any table we tested, so this method is
        a behavioural no-op today; the hook is kept for future DeepDoc
        versions that may return a serialized ``<table>``.
        """
        html = payload.get("html")
        if not html:
            return None
        try:
            from html.parser import HTMLParser

            class _CellCollector(HTMLParser):
                def __init__(self) -> None:
                    super().__init__()
                    self.rows: list[list[str]] = []
                    self._current: list[str] | None = None

                def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
                    if tag in ("tr",):
                        self._current = []
                    elif tag in ("td", "th") and self._current is not None:
                        self._current.append("")

                def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
                    if tag in ("td", "th") and self._current:
                        self._current[-1] = self._current[-1].strip()
                    elif tag == "tr" and self._current is not None:
                        self.rows.append(self._current)
                        self._current = None

                def handle_data(self, data: str) -> None:  # type: ignore[override]
                    if self._current and self._current[-1] != "":
                        return
                    if self._current:
                        self._current[-1] += data

            parser = _CellCollector()
            parser.feed(str(html))
            if not parser.rows:
                return None
            header = parser.rows[0]
            body = parser.rows[1:]
            lines: list[str] = []
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            for row in body:
                # pad / trim to header width so md tables stay square
                cells = (row + [""] * len(header))[: len(header)]
                lines.append("| " + " | ".join(cells) + " |")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 — any HTML parse glitch → None
            logger.warning("TSR html parse failed: %s", exc)
            return None

    def _format_tsr_via_per_cell_ocr(
        self,
        payload: dict[str, Any],
        *,
        original_image: bytes,
        table_bbox: BoundingBox,
        client: Any,
    ) -> str:
        """Crop every cell on the original page image and OCR each one.

        Coordinate system note: TSR returns cell bboxes in the *table
        crop's* coordinate space, not the original page's. We shift
        them by ``table_bbox.{x0,y0}`` before re-cropping from
        ``original_image`` so the OCR-rec call gets the right pixel
        region.

        Returns ``""`` when the per-cell budget is exceeded (caller falls
        back to placeholder) or when no usable cells can be derived.
        """
        bboxes = payload.get("bboxes") or []
        if not bboxes:
            return ""

        # Bucket structural bboxes by class. cls 0 = outer table frame
        # (ignored); cls 1 = column, cls 2 = row, cls 3 = column header,
        # cls 4 = projected row header (left-column), cls 5 = spanning cell.
        columns: list[tuple[float, float, float, float]] = []
        rows: list[tuple[float, float, float, float]] = []
        for box in bboxes:
            if not (isinstance(box, (list, tuple)) and len(box) >= 6):
                continue
            x0, y0, x1, y1 = (float(box[0]), float(box[1]),
                             float(box[2]), float(box[3]))
            cls = round(float(box[5]))
            if cls == 1:  # column
                columns.append((x0, y0, x1, y1))
            elif cls == 2:  # row
                rows.append((x0, y0, x1, y1))

        if not columns or not rows:
            return ""

        # Sort columns left-to-right, rows top-to-bottom so the assembled
        # markdown table reads in the natural reading order.
        columns.sort(key=lambda b: b[0])
        rows.sort(key=lambda b: b[1])

        n_rows = len(rows)
        n_cols = len(columns)
        if n_rows * n_cols > MAX_PER_CELL_OCR_CELLS:
            logger.warning(
                "table cells %d × %d = %d exceeds budget %d — placeholder",
                n_rows, n_cols, n_rows * n_cols, MAX_PER_CELL_OCR_CELLS,
            )
            return ""

        # Per-cell OCR — each cell rect lives in the table crop's coords,
        # shift back to original page coords before cropping.
        matrix: list[list[str]] = []
        for row in rows:
            line: list[str] = []
            for col in columns:
                cell_bbox = BoundingBox(
                    x0=table_bbox.x0 + col[0],
                    y0=table_bbox.y0 + row[1],
                    x1=table_bbox.x0 + col[2],
                    y1=table_bbox.y0 + row[3],
                )
                cell_crop = self._crop(original_image, cell_bbox)
                payload_cell = client.predict_ocr(cell_crop, operator="rec")
                cell_text = self._extract_ocr_text(payload_cell)
                if not cell_text.strip():
                    cell_text = self._rapidocr_on_crop(cell_crop)
                line.append(_WHITESPACE_RUN.sub(" ", cell_text).strip())
            matrix.append(line)

        if not any(any(row) for row in matrix):
            return ""

        lines = ["| " + " | ".join("" for _ in range(n_cols)) + " |"]
        lines.append("| " + " | ".join("---" for _ in range(n_cols)) + " |")
        for row in matrix:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    @staticmethod
    def _placeholder_table_text(payload: dict[str, Any]) -> str:
        """Structural placeholder used when per-cell OCR is skipped / fails.

        Preserves the legacy ``| DeepDoc table | N cols × M rows |`` shape
        so downstream chunkers / consumers can still distinguish a table
        block from prose and surface the structural counts for logging.
        """
        bboxes = payload.get("bboxes") or []
        if not bboxes:
            return ""
        n_rows = 0
        n_cols = 0
        for box in bboxes:
            if not (isinstance(box, (list, tuple)) and len(box) >= 6):
                continue
            cls = round(float(box[5]))
            if cls == 1:
                n_cols += 1
            elif cls == 2:
                n_rows += 1
        if n_rows == 0 and n_cols == 0:
            return f"[DeepDoc table: {len(bboxes)} structural regions]"
        return f"| DeepDoc table | {n_cols} columns × {n_rows} rows |"

    @staticmethod
    def _coerce_bbox(raw: Any) -> BoundingBox | None:
        """Accept DeepDoc's bbox as either a 4-list, 4-tuple, or {x0,y0,x1,y1} dict."""
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            return BoundingBox(x0=float(raw[0]), y0=float(raw[1]),
                               x1=float(raw[2]), y1=float(raw[3]))
        if isinstance(raw, dict):
            try:
                return BoundingBox(
                    x0=float(raw["x0"]), y0=float(raw["y0"]),
                    x1=float(raw["x1"]), y1=float(raw["y1"]),
                )
            except KeyError:
                return None
        return None

    @staticmethod
    def _rapidocr_on_crop(image_bytes: bytes) -> str:
        """Run RapidOCR on a single cropped region.

        Used as a per-box fallback when DeepDoc's OCR-rec returns empty
        text — typically on tall multi-line crops where OCR-rec is
        designed for per-line input and gives up. Returns the joined
        text or ``""`` on any failure (import, runtime, empty result).
        """
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError:
            return ""
        engine = RapidOCR()
        with Image.open(io.BytesIO(image_bytes)) as img:
            tmp_path = Path(tempfile.gettempdir()) / f"ocr_fallback_{id(img)}.jpg"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(tmp_path, format="JPEG")
            try:
                result, _ = engine(str(tmp_path))
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        lines: list[str] = []
        for line in (result or []):
            if len(line) >= 2 and line[1]:
                # Collapse internal whitespace so per-word spans join into a
                # readable paragraph; the chunker / embedder don't care
                # about the original line structure (BGE-M3 tokenizes on
                # whitespace runs either way).
                cleaned = _WHITESPACE_RUN.sub(" ", str(line[1])).strip()
                if cleaned:
                    lines.append(cleaned)
        return " ".join(lines)

    @staticmethod
    def _crop(image_bytes: bytes, bbox: BoundingBox) -> bytes:
        """Crop a JPEG byte buffer to `bbox` (pixel coordinates) and re-encode."""
        with Image.open(io.BytesIO(image_bytes)) as img:
            cropped = img.crop((bbox.x0, bbox.y0, bbox.x1, bbox.y1))
            buffer = io.BytesIO()
            cropped.save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()

    def _rapidocr_fallback(
        self,
        images: Iterable[bytes],
        *,
        document_name: str,
    ) -> ParsedDoc:
        """Run RapidOCR on every page and aggregate into a single fallback section."""
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised in production only
            raise RuntimeError("rapidocr-onnxruntime is not installed") from exc
        engine = RapidOCR()
        all_lines: list[str] = []
        for image_bytes in images:
            with Image.open(io.BytesIO(image_bytes)) as img:
                tmp_path = Path(tempfile.gettempdir()) / f"deepdoc_fallback_{id(img)}.jpg"
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(tmp_path, format="JPEG")
                result, _ = engine(str(tmp_path))
                for line in (result or []):
                    if len(line) >= 2 and line[1]:
                        cleaned = _WHITESPACE_RUN.sub(" ", str(line[1])).strip()
                        if cleaned:
                            all_lines.append(cleaned)
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        if not all_lines:
            raise RuntimeError("RapidOCR produced no text for deepdoc fallback")
        section = ParsedSection(
            heading="(OCR fallback)",
            level=1,
            blocks=[ContentBlock(kind="text", text=" ".join(all_lines), page=0)],
            page_start=0,
            page_end=0,
        )
        return ParsedDoc(
            document_name=document_name,
            format="pdf",
            sections=[section],
            ocr_used=True,
            page_count=1,
        )
