"""Stage 4: push parents + embedded children into Qdrant + Postgres.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Reads ``embedded.json`` produced by ``03_embed.py`` and writes through
the public ``indexer.index(...)`` entry-point. Both stores are mutated
inside a single call:

    * Qdrant ``chunks_v1`` collection — dense + sparse vectors for
      child chunks (parents have no vectors).
    * Postgres ``chunks`` table      — canonical metadata row per
      parent and per child (children carry ``parent_chunk_id`` FK).

Requires:
    * Qdrant reachable at localhost:6333 (override via env)
    * Postgres reachable per ``settings.postgres_*`` (env overrides
      accepted via the standard Settings surface)
    * ``alembic upgrade head`` already applied to the target DB so
      the ``chunks`` table exists.

Usage::

    .venv/Scripts/python.exe tests/integration/manualRun/04_index.py

Output:
    Prints the resulting IndexResult; does not write any JSON.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import QdrantClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agentic_rag_project.db.models.documents import Document  # noqa: E402
from agentic_rag_project.db.models.users import User, Workspace  # noqa: E402
from agentic_rag_project.db.session import SessionLocal  # noqa: E402
from agentic_rag_project.doc_processor.indexer import index  # noqa: E402
from agentic_rag_project.doc_processor.models import (  # noqa: E402
    EmbeddedChunk,
    ParentChunk,
)

from _common import (  # noqa: E402
    DEFAULT_QDRANT_HOST,
    DEFAULT_QDRANT_PORT,
    EMBEDDED_FILE,
    load_json,
    require_file,
)


# Fixed UUIDs for the manual smoke run so re-running the script is
# idempotent without dragging in the full upload API. These are NOT used
# in production — production goes through documents_api.py which creates
# workspace/user/document rows transactionally.
_SMOKE_WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
_SMOKE_OWNER_ID = "22222222-2222-2222-2222-222222222222"


def bootstrap_smoke_doc(document_id: str, document_name: str) -> None:
    """Ensure workspace + owner user + document row exist for the smoke run.

    Idempotent: each entity is checked by id before insert. The chunk
    table's FK on ``chunks.document_id`` requires a parent ``documents``
    row, but ``indexer.index()`` does not create one — this helper fills
    that gap for manual end-to-end testing only.

    Insert order respects FK constraints: ``users`` first (no FKs),
    then ``workspaces`` (owner_id → users.id), then ``documents``
    (workspace_id + owner_id).
    """
    with SessionLocal() as session:
        # 1. Owner user — created first because Workspace.owner_id FKs here.
        owner = session.get(User, _SMOKE_OWNER_ID)
        if owner is None:
            owner = User(
                id=_SMOKE_OWNER_ID,
                username="smoke",
                email="smoke@local",
                password_hash="!smoke-placeholder",
                is_super_admin=True,
            )
            session.add(owner)
            session.flush()

        # 2. Workspace — owner_id references the user above.
        ws = session.get(Workspace, _SMOKE_WORKSPACE_ID)
        if ws is None:
            ws = Workspace(
                id=_SMOKE_WORKSPACE_ID,
                name="smoke-ws",
                owner_id=_SMOKE_OWNER_ID,
            )
            session.add(ws)
            session.flush()

        # 3. Document — workspace_id + owner_id resolve the chunks FK later.
        # Indexer now applies `uuid.UUID(document_id)` (no more hashing —
        # documents.id is a canonical UUID, not a derived UUID5), so the
        # doc seeded here MUST be a real UUID. Pre-2026-09-04 we mirrored
        # indexer._chunk_uuid() to derive a UUID5 — that's the wrong
        # direction now (it would break the chunks.document_id FK).
        doc_uuid = uuid.UUID(document_id)
        existing = session.get(Document, doc_uuid)
        if existing is None:
            session.add(
                Document(
                    id=doc_uuid,
                    workspace_id=_SMOKE_WORKSPACE_ID,
                    owner_id=_SMOKE_OWNER_ID,
                    name=document_name,
                    format="pdf",
                    status="processing",
                )
            )

        session.commit()


def main() -> None:
    """Upsert into Qdrant + Postgres and report the resulting counts."""
    require_file(EMBEDDED_FILE, "embedded.json (stage 3 output)")
    state = load_json(EMBEDDED_FILE)

    doc_id: str = state["doc_id"]
    parents = [ParentChunk.model_validate(p) for p in state["parents"]]
    embedded = [EmbeddedChunk.model_validate(e) for e in state["embedded"]]
    doc_name = state.get("pdf_name") or f"{doc_id}.pdf"

    qdrant_host = os.environ.get("QDRANT_HOST", DEFAULT_QDRANT_HOST)
    qdrant_port = int(os.environ.get("QDRANT_PORT", str(DEFAULT_QDRANT_PORT)))

    print(
        f"[04_index] doc_id={doc_id!r}  parents={len(parents)}  "
        f"embedded={len(embedded)}  qdrant={qdrant_host}:{qdrant_port}"
    )

    if not parents and not embedded:
        print("[04_index] nothing to index — exiting cleanly")
        return

    # Seed workspace/owner/document rows so chunks FK on document_id resolves.
    # Manual-run only — production goes through documents_api.
    bootstrap_smoke_doc(document_id=doc_id, document_name=doc_name)

    qdrant = QdrantClient(host=qdrant_host, port=qdrant_port)
    with SessionLocal() as session:
        result = index(
            parents=parents,
            embedded=embedded,
            document_id=doc_id,
            qdrant=qdrant,
            session=session,
        )

    print(
        f"[04_index] -> parents_written={result.parent_chunks_written}  "
        f"children_written={result.child_chunks_written}  "
        f"qdrant_collection={result.qdrant_collection}  "
        f"pg_chunk_rows={result.pg_chunk_rows}"
    )

    # Quick post-write probe so the user can see the data actually landed.
    count = qdrant.count(collection_name=result.qdrant_collection).count
    print(f"[04_index] qdrant.count({result.qdrant_collection}) = {count}")


if __name__ == "__main__":
    main()