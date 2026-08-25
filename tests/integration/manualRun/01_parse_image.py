"""Stage 1 (image variant): parse a JPG/PNG into a `ParsedDoc` and persist.

Differs from the DOCX/PPTX/XLSX variants: images have no embedded text
layer, so the dispatcher MUST go through the DeepDoc visual router
(DLA + OCR-rec + RapidOCR fallback). This script mirrors ``01_parse.py``
but accepts a ``MANUALRUN_IMAGE`` env var (or auto-picks the first
*.jpg/*.png under test_samples/).

Requires:
    * DeepDoc reachable at ``DEEPDOC_LIVE_URL`` (default
      ``http://localhost:9390``).

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/01_parse_image.py

Optional env:
    MANUALRUN_IMAGE  explicit path to a single image file
    DEEPDOC_LIVE_URL default http://localhost:9390

Output:
    parsed.json  (consumed by 02_chunk.py)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(PROJECT_ROOT))

from agentic_rag_project.doc_processor.parser import (  # noqa: E402
    DeepDocClient,
    parse_document_with_router,
)
from agentic_rag_project.doc_processor.visual_router import (  # noqa: E402
    DeepDocVisualRouter,
)

from _common import (  # noqa: E402
    DEFAULT_DEEPDOC_URL,
    PARSED_FILE,
    dump_json,
    resolve_sample,
)


_IMAGE_SUFFIXES = ("*.jpg", "*.jpeg", "*.png")


def main() -> None:
    """Run one image through the DeepDoc visual router and persist."""
    image = Path(os.environ["MANUALRUN_IMAGE"]) if os.environ.get("MANUALRUN_IMAGE") else resolve_sample(_IMAGE_SUFFIXES)
    deepdoc_url = os.environ.get("DEEPDOC_LIVE_URL", DEFAULT_DEEPDOC_URL)

    print(f"[01_parse_image] IMAGE = {image}")
    print(f"[01_parse_image] DeepDoc URL = {deepdoc_url}")

    # Image extraction MUST go through the visual router; the in-process
    # python-docx / python-pptx / pandas extractors have no text layer to
    # read from a raw bitmap. fallback_ocr=True degrades OCR-rec failures
    # to RapidOCR instead of raising.
    client = DeepDocClient(base_url=deepdoc_url)
    router = DeepDocVisualRouter(client=client, fallback_ocr=True)

    doc = parse_document_with_router(image, visual_router=router)

    n_blocks = sum(len(s.blocks) for s in doc.sections)
    print(
        f"[01_parse_image] -> format={doc.format}  pages={doc.page_count}  "
        f"sections={len(doc.sections)}  blocks={n_blocks}  ocr_used={doc.ocr_used}"
    )
    for idx, sec in enumerate(doc.sections[:3], start=1):
        first_preview = sec.blocks[0].text[:80].replace("\n", " ") if sec.blocks else ""
        print(
            f"[01_parse_image]   section[{idx}] heading={sec.heading!r}  "
            f"blocks={len(sec.blocks)}  first={first_preview!r}"
        )

    payload = {
        "pdf_path": str(image),
        "pdf_name": image.name,
        "doc_id": "smoke-" + image.stem,
        "parsed_doc": doc.model_dump(mode="json"),
    }
    dump_json(PARSED_FILE, payload)
    print(f"[01_parse_image] wrote {PARSED_FILE}")


if __name__ == "__main__":
    main()