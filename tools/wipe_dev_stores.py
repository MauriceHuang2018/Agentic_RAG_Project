"""Wipe dev-stage PG `chunks` + Qdrant `chunks_v1` for clean baseline.

DESTRUCTIVE. Targets only the dev / staging stores (the same host+port
the indexer uses, verified by reading POSTGRES_* and QDRANT_* from the
active ``Settings``). Refuses to run unless ``--yes`` is passed so a
stray invocation cannot lose data.

This is intentionally not a pytest fixture: cleanup is an operational
step, not part of any test contract. Run from the project root::

    .venv/Scripts/python.exe tools/wipe_dev_stores.py --yes

Post-wipe, the next call to ``indexer.index()`` recreates the Qdrant
collection via ``ensure_collection()`` (indexer.py:41) — the index
schema (1024-dim dense + sparse) is reapplied from scratch.

Why DELETE not TRUNCATE for the PG side:
    * TRUNCATE on `chunks` would CASCADE into `documents` because of the
      FK constraint (``documents.id`` referenced by ``chunks.document_id``
      ON DELETE CASCADE — see documents.py:104). We only want to clear
      chunks; documents are managed by the ingest workflow.
    * DELETE is per-row so it respects CASCADE on the self-referential
      ``parent_chunk_id`` FK (parents before children), but flush() in
      indexer always inserts parents first, so a single DELETE FROM is
      safe — the CASCADE only matters if there were orphans.

Safety invariants (enforced in code):
    * Pydantic Settings must point at the configured dev stores
      (``localhost`` for Qdrant, the local PG DSN). If either host
      looks production-like (non-localhost / external domain), the
      script refuses to run.
    * Prints before/after row counts so the caller sees what was lost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo-root importable so `agentic_rag_project` resolves without packaging.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sqlalchemy import text  # noqa: E402

from agentic_rag_project.config import get_settings  # noqa: E402
from agentic_rag_project.db.session import SessionLocal, get_engine  # noqa: E402


def _is_local_target(host: str, port: int) -> bool:
    """Return True only for loopback / docker-bridge targets (dev hosts)."""
    local_hosts = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    if host in local_hosts:
        return True
    # Docker-bridge IP ranges commonly used by host→container setups.
    if host.startswith("172.17.") or host.startswith("172.18."):
        return True
    return False


def _wipe_pg() -> tuple[int, int]:
    """Delete every row from PG ``chunks``. Return (before, after)."""
    before = 0
    with SessionLocal() as session:
        before = session.execute(text("SELECT count(*) FROM chunks")).scalar() or 0
        session.execute(text("DELETE FROM chunks"))
        session.commit()
    with SessionLocal() as session:
        after = session.execute(text("SELECT count(*) FROM chunks")).scalar() or 0
    return before, after


def _wipe_qdrant(collection: str) -> tuple[bool, int | None]:
    """Drop the Qdrant collection. Return (existed_before, points_count)."""
    from qdrant_client import QdrantClient  # local import: keep CLI snappy

    settings = get_settings()
    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    existed = client.collection_exists(collection_name=collection)
    points = None
    if existed:
        try:
            info = client.get_collection(collection_name=collection)
            points = info.points_count
        except Exception:
            points = None  # collection in a weird state; delete still safe
        client.delete_collection(collection_name=collection)
    return existed, points


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation flag. Without it the script only prints plans.",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Qdrant collection name (default: settings.qdrant_collection).",
    )
    args = parser.parse_args()

    settings = get_settings()
    collection = args.collection or settings.qdrant_collection

    print(f"[wipe] target Qdrant  = {settings.qdrant_host}:{settings.qdrant_port} / {collection}")
    print(
        f"[wipe] target PG     = {settings.postgres_host}:{settings.postgres_port}"
        f" / db={settings.postgres_db}"
    )

    if not _is_local_target(settings.qdrant_host, settings.qdrant_port):
        sys.exit(
            f"refusing to wipe non-local Qdrant host {settings.qdrant_host!r}; "
            "this tool is dev-only."
        )
    if not _is_local_target(settings.postgres_host, settings.postgres_port):
        sys.exit(
            f"refusing to wipe non-local PG host {settings.postgres_host!r}; "
            "this tool is dev-only."
        )

    if not args.yes:
        print("[wipe] dry-run only — pass --yes to actually wipe.")
        # Probe-only: show what would happen, then exit 0.
        with SessionLocal() as s:
            n = s.execute(text("SELECT count(*) FROM chunks")).scalar() or 0
        print(f"[wipe]   would delete {n} rows from PG chunks")
        try:
            from qdrant_client import QdrantClient

            qc = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
            if qc.collection_exists(collection_name=collection):
                info = qc.get_collection(collection_name=collection)
                print(
                    f"[wipe]   would drop Qdrant collection {collection!r} "
                    f"({info.points_count} points)"
                )
            else:
                print(f"[wipe]   Qdrant collection {collection!r} does not exist")
        except Exception as exc:
            print(f"[wipe]   could not probe Qdrant: {exc}")
        return

    # Touch engine so the lazy builder is exercised against the same DSN we just gated.
    _ = get_engine()

    print("[wipe] wiping PG chunks ...")
    pg_before, pg_after = _wipe_pg()
    print(f"[wipe]   chunks: {pg_before} -> {pg_after}")

    print("[wipe] wiping Qdrant collection ...")
    q_existed, q_points = _wipe_qdrant(collection)
    print(
        f"[wipe]   {collection}: existed={q_existed} "
        f"points_before={q_points}"
    )
    print("[wipe] done. Next indexer call will recreate the Qdrant collection.")


if __name__ == "__main__":
    main()
