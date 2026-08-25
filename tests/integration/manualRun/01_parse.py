"""Stage 1: parse a PDF into a `ParsedDoc` and persist to `parsed.json`.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Pipeline entry-point exercised here:
    parse_document_with_router(pdf, visual_router=...)
        -> ParsedDoc

The dispatcher auto-detects scan vs text-layer:
    * scanned / complex PDFs  -> DeepDoc visual router (DLA + OCR + TSR)
    * text-layer PDFs          -> PyMuPDF (no OCR)

`fallback_ocr=True` matches the live integration fixture and protects
against DeepDoc pages whose DLA boxes return empty text from OCR-rec.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/01_parse.py

Optional env:
    DEEPDOC_LIVE_URL  default http://localhost:9390

Output:
    parsed.json  (consumed by 02_chunk.py)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `agentic_rag_project` importable when run from any cwd.
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


def main() -> None:
    """Run one PDF through the format dispatcher and persist the result."""
    pdf = resolve_sample("*.pdf")
    deepdoc_url = os.environ.get("DEEPDOC_LIVE_URL", DEFAULT_DEEPDOC_URL)

    print(f"[01_parse] PDF          = {pdf}")
    print(f"[01_parse] DeepDoc URL  = {deepdoc_url}")

    # Build a real DeepDoc client + visual router. fallback_ocr=True so a
    # transient DLA / OCR-rec failure degrades to RapidOCR instead of
    # raising — matches production behaviour exercised by the live tests.
    client = DeepDocClient(base_url=deepdoc_url)
    router = DeepDocVisualRouter(client=client, fallback_ocr=True)

    doc = parse_document_with_router(pdf, visual_router=router)

    # Surface the headline numbers so the user can sanity-check before
    # proceeding to stage 2.
    n_blocks = sum(len(s.blocks) for s in doc.sections)
    print(
        f"[01_parse] -> format={doc.format}  pages={doc.page_count}  "
        f"sections={len(doc.sections)}  blocks={n_blocks}  ocr_used={doc.ocr_used}"
    )
    for idx, sec in enumerate(doc.sections[:3], start=1):
        first_preview = sec.blocks[0].text[:80].replace("\n", " ") if sec.blocks else ""
        print(
            f"[01_parse]   section[{idx}] heading={sec.heading!r}  "
            f"blocks={len(sec.blocks)}  first={first_preview!r}"
        )

    # Persist for stage 2. JSON-safe via pydantic's mode='json' serializer.
    payload = {
        "pdf_path": str(pdf),
        "pdf_name": pdf.name,
        "doc_id": "smoke-" + pdf.stem,
        "parsed_doc": doc.model_dump(mode="json"),
    }
    dump_json(PARSED_FILE, payload)
    print(f"[01_parse] wrote {PARSED_FILE}")


if __name__ == "__main__":
    main()