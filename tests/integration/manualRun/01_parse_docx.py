"""Stage 1 (DOCX variant): parse a DOCX into a `ParsedDoc` and persist.

Mirrors ``01_parse.py`` but feeds the dispatcher a DOCX path instead of
a PDF. ``parse_document_with_router`` auto-detects the extension and
routes DOCX to the in-process ``extract_docx`` (python-docx) — no
DeepDoc round-trip, no OCR. Stages 2-4 are format-agnostic and consume
the JSON this script writes.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/01_parse_docx.py

Optional env:
    MANUALRUN_DOCX  default = first *.docx under test_samples/ (sorted)

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
    parse_document_with_router,
)

from _common import (  # noqa: E402
    PARSED_FILE,
    dump_json,
    resolve_sample,
)


def main() -> None:
    """Parse one DOCX through the format dispatcher and persist."""
    docx = Path(os.environ["MANUALRUN_DOCX"]) if os.environ.get("MANUALRUN_DOCX") else resolve_sample("*.docx")

    print(f"[01_parse_docx] DOCX = {docx}")

    # DOCX path bypasses DeepDoc entirely; the dispatcher signature still
    # accepts a visual_router kwarg but it's ignored for extract_docx.
    doc = parse_document_with_router(docx)

    n_blocks = sum(len(s.blocks) for s in doc.sections)
    print(
        f"[01_parse_docx] -> format={doc.format}  pages={doc.page_count}  "
        f"sections={len(doc.sections)}  blocks={n_blocks}  ocr_used={doc.ocr_used}"
    )
    for idx, sec in enumerate(doc.sections[:3], start=1):
        first_preview = sec.blocks[0].text[:80].replace("\n", " ") if sec.blocks else ""
        print(
            f"[01_parse_docx]   section[{idx}] heading={sec.heading!r}  "
            f"blocks={len(sec.blocks)}  first={first_preview!r}"
        )

    payload = {
        "pdf_path": str(docx),
        "pdf_name": docx.name,
        "doc_id": "smoke-" + docx.stem,
        "parsed_doc": doc.model_dump(mode="json"),
    }
    dump_json(PARSED_FILE, payload)
    print(f"[01_parse_docx] wrote {PARSED_FILE}")


if __name__ == "__main__":
    main()