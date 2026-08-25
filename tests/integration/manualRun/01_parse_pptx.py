"""Stage 1 (PPTX variant): parse a PPTX into a `ParsedDoc` and persist.

Mirrors ``01_parse_docx.py`` but feeds the dispatcher a PPTX path. The
dispatcher auto-detects by extension and routes PPTX to the in-process
``extract_pptx`` (python-pptx) — no DeepDoc round-trip, no OCR. Stages
2-4 are format-agnostic and consume the JSON this script writes.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/01_parse_pptx.py

Optional env:
    MANUALRUN_PPTX  default = first *.pptx under test_samples/ (sorted)

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
    """Parse one PPTX through the format dispatcher and persist."""
    pptx = Path(os.environ["MANUALRUN_PPTX"]) if os.environ.get("MANUALRUN_PPTX") else resolve_sample("*.pptx")

    print(f"[01_parse_pptx] PPTX = {pptx}")

    doc = parse_document_with_router(pptx)

    n_blocks = sum(len(s.blocks) for s in doc.sections)
    print(
        f"[01_parse_pptx] -> format={doc.format}  pages={doc.page_count}  "
        f"sections={len(doc.sections)}  blocks={n_blocks}  ocr_used={doc.ocr_used}"
    )
    for idx, sec in enumerate(doc.sections[:3], start=1):
        first_preview = sec.blocks[0].text[:80].replace("\n", " ") if sec.blocks else ""
        print(
            f"[01_parse_pptx]   section[{idx}] heading={sec.heading!r}  "
            f"blocks={len(sec.blocks)}  first={first_preview!r}"
        )

    payload = {
        "pdf_path": str(pptx),
        "pdf_name": pptx.name,
        "doc_id": "smoke-" + pptx.stem,
        "parsed_doc": doc.model_dump(mode="json"),
    }
    dump_json(PARSED_FILE, payload)
    print(f"[01_parse_pptx] wrote {PARSED_FILE}")


if __name__ == "__main__":
    main()