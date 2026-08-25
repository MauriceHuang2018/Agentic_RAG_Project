"""Stage 1 (XLSX variant): parse an XLSX into a `ParsedDoc` and persist.

Mirrors ``01_parse_docx.py`` but feeds the dispatcher an XLSX path. The
dispatcher auto-detects by extension and routes XLSX to the in-process
``extract_xlsx`` (pandas) — no DeepDoc round-trip, no OCR. Stages 2-4
are format-agnostic and consume the JSON this script writes.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/01_parse_xlsx.py

Optional env:
    MANUALRUN_XLSX  default = first *.xlsx under test_samples/ (sorted)

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
    """Parse one XLSX through the format dispatcher and persist."""
    xlsx = Path(os.environ["MANUALRUN_XLSX"]) if os.environ.get("MANUALRUN_XLSX") else resolve_sample("*.xlsx")

    print(f"[01_parse_xlsx] XLSX = {xlsx}")

    doc = parse_document_with_router(xlsx)

    n_blocks = sum(len(s.blocks) for s in doc.sections)
    print(
        f"[01_parse_xlsx] -> format={doc.format}  pages={doc.page_count}  "
        f"sections={len(doc.sections)}  blocks={n_blocks}  ocr_used={doc.ocr_used}"
    )
    for idx, sec in enumerate(doc.sections[:3], start=1):
        first_preview = sec.blocks[0].text[:80].replace("\n", " ") if sec.blocks else ""
        print(
            f"[01_parse_xlsx]   section[{idx}] heading={sec.heading!r}  "
            f"blocks={len(sec.blocks)}  first={first_preview!r}"
        )

    payload = {
        "pdf_path": str(xlsx),
        "pdf_name": xlsx.name,
        "doc_id": "smoke-" + xlsx.stem,
        "parsed_doc": doc.model_dump(mode="json"),
    }
    dump_json(PARSED_FILE, payload)
    print(f"[01_parse_xlsx] wrote {PARSED_FILE}")


if __name__ == "__main__":
    main()