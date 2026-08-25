"""Stage 3: embed children via LiteLLM BGE-M3 and persist `EmbeddedChunk`s.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Reads ``chunks.json`` produced by ``02_chunk.py`` and produces
``embedded.json``. Only children carry vectors — parents stay text-only
context units and are passed through verbatim for stage 4 (the indexer
still needs them for the parent-child Postgres join).

Requires LiteLLM reachable at ``settings.litellm_base_url`` (default
``http://localhost:4000``) with the BGE-M3 model loaded. Override via
env if your stack exposes the embedding proxy elsewhere.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/03_embed.py

Output:
    embedded.json  (consumed by 04_index.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(PROJECT_ROOT))

from agentic_rag_project.doc_processor.embedder import (  # noqa: E402
    LiteLLMEmbedder,
)
from agentic_rag_project.doc_processor.models import (  # noqa: E402
    ChildChunk,
    EmbeddedChunk,
    ParentChunk,
)

from _common import (  # noqa: E402
    CHUNKS_FILE,
    EMBEDDED_FILE,
    dump_json,
    load_json,
    require_file,
)


def main() -> None:
    """Embed children with LiteLLM BGE-M3 and persist with parents."""
    require_file(CHUNKS_FILE, "chunks.json (stage 2 output)")
    state = load_json(CHUNKS_FILE)

    doc_id: str = state["doc_id"]
    parents = [ParentChunk.model_validate(p) for p in state["parents"]]
    children = [ChildChunk.model_validate(c) for c in state["children"]]

    if not children:
        print("[03_embed] no children to embed — exiting cleanly")
        # Still write an empty payload so 04_index.py can short-circuit.
        dump_json(
            EMBEDDED_FILE,
            {"doc_id": doc_id, "parents": [], "embedded": []},
        )
        return

    embedder = LiteLLMEmbedder()  # reads settings.litellm_base_url + model
    embedded: list[EmbeddedChunk] = embedder.embed_chunks(children)

    # Sanity print — dense dim is fixed at 1024 by the BGE-M3 contract.
    dense_dim = len(embedded[0].dense_vector)
    sparse_nz = len(embedded[0].sparse_vector)
    print(
        f"[03_embed] doc_id={doc_id!r}  embedded={len(embedded)}  "
        f"dense_dim={dense_dim}  sparse_nz={sparse_nz}"
    )
    if embedded:
        preview_dense = [round(x, 4) for x in embedded[0].dense_vector[:5]]
        preview_sparse = dict(list(embedded[0].sparse_vector.items())[:3])
        print(
            f"[03_embed]   first.dense[0:5] = {preview_dense}  "
            f"first.sparse (3 keys) = {preview_sparse}"
        )

    # Persist parents + embedded children — indexer needs both.
    payload = {
        "doc_id": doc_id,
        "parents": [p.model_dump(mode="json") for p in parents],
        "embedded": [e.model_dump(mode="json") for e in embedded],
    }
    dump_json(EMBEDDED_FILE, payload)
    print(f"[03_embed] wrote {EMBEDDED_FILE}")


if __name__ == "__main__":
    main()