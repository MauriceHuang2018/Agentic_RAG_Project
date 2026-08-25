"""Diagnostic: show what /predict/tsr returns for each class-5 table box.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

``diag_dla.py`` only probes /predict/dla — it tells us WHERE the boxes
are but nothing about what /predict/tsr hands back for them. This
companion probes TSR's actual payload shape so we can decide whether
the HTML fast-path is real or dead code before sinking engineering
into a BeautifulSoup adapter.

For every class-5 box found by DLA, we POST the cropped region to
/predict/tsr and print:

    * html field — present? length? first 200 chars
    * bboxes field — count + per-class breakdown
      (column / row / column_header / projected_row_header / spanning_cell)
    * any other top-level keys

Run::

    .venv/Scripts/python.exe tests/integration/manualRun/diag_tsr.py
"""

from __future__ import annotations

import io
import os
import sys
from collections import Counter
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


# Map TSR's ``bboxes[5]`` class_id -> human label. The exact convention
# is *not* verified against the live server; the diag prints the raw
# counts so any mismatch is obvious in the output.
TSR_CLASS_LABELS: dict[int, str] = {
    0: "table",
    1: "column",
    2: "row",
    3: "column_header",
    4: "projected_row_header",
    5: "spanning_cell",
}


def _crop(image_bytes: bytes, bbox: tuple[float, float, float, float]) -> bytes:
    """Crop a JPEG byte buffer to ``bbox`` (visual_router._crop, inlined)."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        cropped = img.crop((bbox[0], bbox[1], bbox[2], bbox[3]))
        buffer = io.BytesIO()
        cropped.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()


def _print_payload_summary(payload: dict) -> None:
    """Print a one-screen summary of a TSR payload."""
    keys = sorted(payload.keys())
    print(f"    keys: {keys}")

    # HTML fast-path diagnostic — the question this script exists to answer.
    html = payload.get("html")
    if html:
        preview = html[:200].replace("\n", " ")
        print(f"    html.present=True  length={len(html)}  preview={preview!r}")
    else:
        print("    html.present=False  (HTML fast-path is dead code)")

    # Structural bboxes breakdown — confirms the per-cell OCR plan's
    # row/column class assumptions.
    bboxes = payload.get("bboxes") or []
    counts: Counter[int] = Counter()
    for box in bboxes:
        if isinstance(box, (list, tuple)) and len(box) >= 6:
            counts[round(float(box[5]))] += 1
    print(f"    bboxes.count={len(bboxes)}  per-class={dict(counts)}")
    if counts:
        print("    per-class labels:")
        for cls_int in sorted(counts):
            label = TSR_CLASS_LABELS.get(cls_int, f"unknown({cls_int})")
            print(f"      {cls_int}={label}: {counts[cls_int]}")


def main() -> None:
    """Render every page, dump TSR payloads for each class-5 box."""
    pdf = resolve_pdf_sample()
    deepdoc_url = os.environ.get("DEEPDOC_LIVE_URL", DEFAULT_DEEPDOC_URL)
    client = DeepDocClient(base_url=deepdoc_url)

    print(f"PDF: {pdf}")
    print(f"DeepDoc URL: {deepdoc_url}\n")

    page_images = render_pdf_pages_to_images(pdf, dpi=150)
    print(f"Rendered {len(page_images)} page(s) at 150dpi\n")

    total_table_boxes = 0
    total_html_present = 0

    for page_idx, image_bytes in enumerate(page_images):
        layout = client.predict_dla(image_bytes)
        boxes = layout.get("bboxes") or layout.get("boxes") or []
        table_boxes = [
            b for b in boxes
            if isinstance(b, (list, tuple)) and len(b) >= 6 and round(float(b[5])) == 5
        ]
        if not table_boxes:
            continue
        print(f"========== PAGE {page_idx + 1} ({len(table_boxes)} table box(es)) ==========")
        for box_idx, box in enumerate(table_boxes):
            x0, y0, x1, y1 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            score = float(box[4])
            print(
                f"\n  [box {box_idx}] bbox=({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f})  "
                f"score={score:.3f}"
            )
            cropped = _crop(image_bytes, (x0, y0, x1, y1))
            payload = client.predict_tsr(cropped)
            _print_payload_summary(payload)
            total_table_boxes += 1
            if payload.get("html"):
                total_html_present += 1

    print(
        f"\n========== SUMMARY ==========\n"
        f"Total class-5 boxes probed: {total_table_boxes}\n"
        f"HTML present in payload:   {total_html_present} "
        f"({(100 * total_html_present / total_table_boxes):.0f}% of tables)"
        if total_table_boxes
        else "\n========== SUMMARY ==========\nNo class-5 boxes found in this PDF."
    )


if __name__ == "__main__":
    main()