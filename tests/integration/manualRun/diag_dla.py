"""Diagnostic: show what /predict/dla returns for each page of scanned_pdf.pdf.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Stage 1 dropped some content on the photocell paragraph. Three suspects:

    A. DLA classified the box as non-text (class 2/3/4/6/8) and
       ``_DLA_CLASS_TEXT | _DLA_CLASS_TABLE`` filters it out.
    B. DLA returned a text box but the bbox is misaligned so OCR-rec
       gets a blank crop and the empty-string guard drops the block.
    C. DLA did not return a box for that paragraph at all.

This script renders every page of ``scanned_pdf.pdf`` to JPEG, POSTs
each to ``/predict/dla``, prints every box with class_id + bbox + score,
and re-runs OCR-rec on the text-class boxes so we can see exactly what
the visual_router's downstream calls would see.

Run::

    .venv/Scripts/python.exe tests/integration/manualRun/diag_dla.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(PROJECT_ROOT))

from agentic_rag_project.doc_processor.parser import DeepDocClient  # noqa: E402
from agentic_rag_project.doc_processor.text_extractors.pdf_text import (  # noqa: E402
    render_pdf_pages_to_images,
)

from _common import (  # noqa: E402
    DEFAULT_DEEPDOC_URL,
    resolve_pdf_sample,
)


# Map class_id -> human label. Matches the visual_router convention.
CLASS_LABELS: dict[int, str] = {
    0: "title",
    1: "text",
    2: "reference",
    3: "figure",
    4: "figure_caption",
    5: "table",
    6: "table_caption",
    8: "equation",
}
# Classes the visual_router keeps (route to OCR-rec or TSR).
KEPT_CLASSES: frozenset[int] = frozenset({0, 1, 5})


def _crop(image_bytes: bytes, bbox: tuple[float, float, float, float]) -> bytes:
    """Return a JPEG of the bbox region (visual_router._crop, inlined)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        cropped = img.crop((bbox[0], bbox[1], bbox[2], bbox[3]))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def main() -> None:
    """Render every page, dump DLA boxes + OCR text for text boxes."""
    pdf = resolve_pdf_sample()
    deepdoc_url = os.environ.get("DEEPDOC_LIVE_URL", DEFAULT_DEEPDOC_URL)
    client = DeepDocClient(base_url=deepdoc_url)

    print(f"PDF: {pdf}")
    print(f"DeepDoc URL: {deepdoc_url}\n")

    page_images = render_pdf_pages_to_images(pdf, dpi=150)
    print(f"Rendered {len(page_images)} page(s) at 150dpi\n")

    for page_idx, image_bytes in enumerate(page_images):
        print(f"========== PAGE {page_idx + 1} ==========")
        layout = client.predict_dla(image_bytes)
        boxes = layout.get("bboxes") or layout.get("boxes") or []
        print(f"DLA returned {len(boxes)} box(es)\n")

        for box_idx, box in enumerate(boxes):
            if not (isinstance(box, (list, tuple)) and len(box) >= 6):
                print(f"  [{box_idx}] malformed box: {box!r}")
                continue
            x0, y0, x1, y1, score, class_id = (
                float(box[0]), float(box[1]), float(box[2]),
                float(box[3]), float(box[4]), float(box[5]),
            )
            cls_int = round(class_id)
            label = CLASS_LABELS.get(cls_int, f"unknown({cls_int})")
            kept = "KEEP" if cls_int in KEPT_CLASSES else "DROP"
            print(
                f"  [{box_idx}] {kept}  cls={cls_int}({label})  "
                f"score={score:.3f}  bbox=({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f})"
            )

            # For text/title boxes, run OCR-rec on the crop so we see the
            # exact text the visual_router would feed into a ContentBlock.
            if cls_int in {0, 1}:
                cropped = _crop(image_bytes, (x0, y0, x1, y1))
                ocr = client.predict_ocr(cropped, operator="rec")
                items = ((ocr.get("output") or [[]])[0] or [[]])[0] or []
                lines: list[str] = []
                for item in items:
                    if isinstance(item, (list, tuple)) and item:
                        lines.append(str(item[0]).strip())
                    elif isinstance(item, str):
                        lines.append(item.strip())
                joined = " | ".join(line for line in lines if line)
                preview = joined[:120] + ("..." if len(joined) > 120 else "")
                flag = "" if joined else "  <-- EMPTY (visual_router would DROP this)"
                print(f"        OCR: {preview!r}{flag}")

        print()  # blank line between pages


if __name__ == "__main__":
    main()