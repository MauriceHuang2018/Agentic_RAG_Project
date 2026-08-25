"""Stage 2: chunk a `ParsedDoc` into (parents, children) and persist.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Reads ``parsed.json`` produced by ``01_parse.py`` and produces
``chunks.json`` containing both the parent list and the child list.
Stage 3 (``03_embed.py``) reads this file and only embeds the children
— parents are pure context units and never carry vectors.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/02_chunk.py

Output:
    chunks.json  (consumed by 03_embed.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(PROJECT_ROOT))

from agentic_rag_project.doc_processor.chunker import chunk_parsed_doc  # noqa: E402
from agentic_rag_project.doc_processor.models import (  # noqa: E402
    ChildChunk,
    ParentChunk,
    ParsedDoc,
)

from _common import (  # noqa: E402
    CHUNKS_FILE,
    PARSED_FILE,
    dump_json,
    load_json,
    require_file,
)


def main() -> None:
    """Reconstruct the ParsedDoc, run chunk_parsed_doc, persist both lists."""
    require_file(PARSED_FILE, "parsed.json (stage 1 output)")
    parsed_state = load_json(PARSED_FILE)

    # Rehydrate pydantic models from the JSON payload. Model_validate
    # honours the field types so any drift between stages surfaces here.
    parsed = ParsedDoc.model_validate(parsed_state["parsed_doc"])
    doc_id = parsed_state["doc_id"]

    parents: list[ParentChunk]
    children: list[ChildChunk]
    parents, children = chunk_parsed_doc(parsed, document_id=doc_id)

    print(
        f"[02_chunk] doc_id={doc_id!r}  parents={len(parents)}  children={len(children)}"
    )
    for idx, p in enumerate(parents[:5], start=1):
        print(
            f"[02_chunk]   parent[{idx}] heading={p.section_heading!r}  "
            f"chars={len(p.content)}  children={len(p.child_ids)}  "
            f"pages={p.page_start}-{p.page_end}"
        )
    if children:
        first = children[0]
        print(
            f"[02_chunk]   child[0] kind={first.block_kind}  page={first.page}  "
            f"chars={len(first.content)}  preview={first.content[:60]!r}"
        )

    # Persist both lists for stage 3.
    payload = {
        "doc_id": doc_id,
        "parents": [p.model_dump(mode="json") for p in parents],
        "children": [c.model_dump(mode="json") for c in children],
    }
    dump_json(CHUNKS_FILE, payload)
    print(f"[02_chunk] wrote {CHUNKS_FILE}")


if __name__ == "__main__":
    main()