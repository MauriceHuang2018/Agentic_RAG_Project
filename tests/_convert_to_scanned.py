"""Convert a text-PDF into a scanned-style PDF (no embedded text layer).

Each page is rendered to a JPEG via PyMuPDF, then the JPEGs are
re-embedded as a fresh PDF with no /Font / /Contents text stream —
so PyMuPDF's text-layer extraction returns empty, forcing the
pipeline down the DeepDoc visual_router path (DLA + OCR-rec + TSR).

Lives under ``tests/`` (not ``tests/integration/manualRun/``) because
it is a one-shot fixture-prep tool, not a stage of the smoke pipeline.
Re-running it is idempotent: it skips files already ending in
``.scanned.pdf``.

Usage::

    .venv/Scripts/python.exe tests/_convert_to_scanned.py

Input : ``test_samples/*.pdf`` (skips ``*.scanned.pdf``)
Output: ``test_samples/<name>.scanned.pdf``
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf  # PyMuPDF (formerly ``import fitz``)

# tests/_convert_to_scanned.py → parents[1] = project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = PROJECT_ROOT / "test_samples"


def convert_to_scanned(src: Path, dst: Path, *, dpi: int = 150) -> None:
    """Re-pack every page of ``src`` as a JPEG-only PDF with no text."""
    doc = pymupdf.open(str(src))
    out = pymupdf.open()
    zoom = dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)
    for page in doc:
        # Render page to a pixmap (RGB bytes), no vector text preserved.
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img_bytes = pix.tobytes("jpeg")
        # Build a fresh page sized to the image (in PDF points / 72dpi).
        # page.rect is in points; pix.width is in image pixels = points*zoom.
        width_pt = pix.width / zoom
        height_pt = pix.height / zoom
        new_page = out.new_page(width=width_pt, height=height_pt)
        new_page.insert_image(new_page.rect, stream=img_bytes)
    out.save(str(dst), garbage=4, deflate=True, clean=True)
    out.close()
    doc.close()


def main() -> None:
    """Convert every non-scanned PDF in test_samples/ to a scanned twin.

    Idempotent: skip files whose ``.scanned.pdf`` twin already exists
    unless ``--force`` is passed. Skips files already named
    ``*.scanned.pdf`` outright (they are outputs, not inputs).
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-convert even when the .scanned.pdf twin already exists.",
    )
    args = parser.parse_args()

    matches = sorted(SAMPLES_DIR.glob("*.pdf"))
    if not matches:
        sys.exit(f"no *.pdf in {SAMPLES_DIR}")
    for src in matches:
        if src.stem.endswith(".scanned"):
            continue  # this IS a scanned twin, not an input
        dst = SAMPLES_DIR / f"{src.stem}.scanned.pdf"
        if dst.exists() and not args.force:
            print(f"[scanned] skip (exists): {dst.name}")
            continue
        print(f"[scanned] converting {src.name} -> {dst.name}")
        convert_to_scanned(src, dst)
        print(f"[scanned] wrote {dst} ({dst.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()