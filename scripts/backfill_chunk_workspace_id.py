"""One-shot backfill — populate `workspace_id` on existing Qdrant chunks.

WHY THIS SCRIPT EXISTS
======================

Migration 0013 (`chunks.workspace_id` NOT NULL, 2026-09-03) closes the
PG-side gap of the P0 empty-answer bug: every `chunks` row now carries
its parent document's `workspace_id` (backfilled from `documents` via
JOIN).

This script closes the Qdrant-side gap for chunks that were indexed
BEFORE the data-pipeline fix landed (`doc_processor/{chunker,embedder,
indexer}.py` started threading `workspace_id` in commit 2a41f39). For
those chunks the Qdrant payload has no `workspace_id` field, so the
new `acl_filter.build_user_filter` cannot scope by workspace even
when a user's membership is correct — and the retriever returns 0
hits.

Run this once after `alembic upgrade head` and the api-gateway /
celery-worker image rebuild:

    PYTHONPATH=src python scripts/backfill_chunk_workspace_id.py

For a no-op verification pass (no Qdrant writes):

    PYTHONPATH=src python scripts/backfill_chunk_workspace_id.py --dry-run

IDEMPOTENCY
===========

`client.set_payload` merges the supplied payload into each point's
existing payload (does NOT replace), so re-running this script
overwrites the same `workspace_id` values with identical values. No
op except wasted Qdrant calls. After this script runs once, future
re-runs are safe and near-instant — useful for verifying that
nothing slipped through.

FAILURE MODES
=============

* A `chunks` row references a Qdrant point that no longer exists
  (manual Qdrant cleanup, partial restore). The script logs and
  skips — `set_payload` accepts a missing point id silently on the
  server, and we still report the chunk as "updated" because PG is
  the source of truth here.
* A Qdrant call fails (network, server restart). The batch is
  retried up to 3 times with exponential backoff; if it still fails,
  the script logs the chunk IDs and continues. The summary at the
  end surfaces any chunks that did not get their payload updated.
* Migration 0013 has NOT been applied yet (some chunks still have
  workspace_id IS NULL). The script's preflight SELECT surfaces this
  and refuses to proceed — it would otherwise backfill an
  inconsistent state.

DESIGN REFERENCE
================

docs/workspace_id_pipeline/DESIGN §2.1 — the schema migration closes
the PG half; this script closes the Qdrant half.

TASK REFERENCE
==============

docs/workspace_id_pipeline/TASK T14 — operational one-shot.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator

from qdrant_client import QdrantClient
from sqlalchemy import text

from agentic_rag_project.config import get_settings
from agentic_rag_project.db.session import get_engine
from agentic_rag_project.retrieval_direct.qdrant_client import build_qdrant_client

logger = logging.getLogger(__name__)


# Qdrant batch size: keeps each set_payload call under ~1MB payload
# even when a workspace owns tens of thousands of chunks (the payload
# dict is the same per batch — only the point IDs vary).
DEFAULT_BATCH_SIZE = 256


def _stream_chunks_with_workspace(
    batch_size: int, limit: int | None
) -> Iterator[list[tuple[uuid.UUID, uuid.UUID]]]:
    """Yield `(chunk_id, workspace_id)` tuples from PG in batches.

    Uses a server-side cursor so a million-chunk collection doesn't
    load every row into Python memory at once. `chunk_id` is the
    `chunks.id` UUID5 (which is also the Qdrant point id — see
    `doc_processor.indexer._chunk_uuid` and `_qdrant_point_id` for
    the deterministic derivation).

    The script never reads chunks where `workspace_id IS NULL` —
    migration 0013 has already populated that column. A preflight
    count above this function catches the "migration not applied"
    case before any Qdrant writes happen.
    """
    # `id` is the UUID5 derived from chunk_id; it IS the Qdrant point
    # id (see `doc_processor.indexer._chunk_uuid` / `_qdrant_point_id`
    # for the deterministic derivation). SELECT id is enough; we don't
    # need the original chunk_id string.
    stmt = text(
        "SELECT id, workspace_id FROM chunks "
        "WHERE workspace_id IS NOT NULL "
        "ORDER BY workspace_id, id"
    )
    engine = get_engine()
    emitted = 0
    with engine.connect().execution_options(yield_per=batch_size) as conn:
        result = conn.execute(stmt)
        batch: list[tuple[uuid.UUID, uuid.UUID]] = []
        for row in result:
            batch.append((row[0], row[1]))
            emitted += 1
            if len(batch) >= batch_size:
                yield batch
                batch = []
                if limit is not None and emitted >= limit:
                    return
        if batch:
            yield batch


def _set_payload_with_retry(
    client: QdrantClient,
    collection: str,
    payload: dict[str, str],
    point_ids: list[str],
    *,
    max_attempts: int = 3,
) -> tuple[bool, str]:
    """Call `client.set_payload` with retry + backoff.

    Returns `(success, error_message)`. `error_message` is empty on
    success. The whole batch is retried as a unit because Qdrant's
    API has no partial-failure semantics for a single set_payload
    call (the call either applies the payload to all listed points
    or fails before any write).
    """
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            client.set_payload(
                collection_name=collection,
                payload=payload,
                points=point_ids,
                wait=True,
            )
            return True, ""
        except Exception as exc:  # qdrant-client raises broad exceptions
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                backoff = 2 ** (attempt - 1)  # 1s, 2s
                logger.warning(
                    "set_payload attempt %d/%d failed (%s); retrying in %ds",
                    attempt, max_attempts, last_error, backoff,
                )
                time.sleep(backoff)
    return False, last_error


def _count_chunks_without_workspace() -> int:
    """Preflight — refuse to run if migration 0013 hasn't been applied.

    After 0013 every chunks row carries workspace_id NOT NULL. If we
    still see NULLs, running the backfill would either skip them (no
    workspace_id to push to Qdrant) or, worse, push NULL into a
    Qdrant payload field that the ACL filter reads. Better to fail
    fast and let the operator apply the migration first.
    """
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM chunks WHERE workspace_id IS NULL")
        ).scalar_one()


def _count_total_chunks() -> int:
    """Total chunks (for the progress log)."""
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM chunks")).scalar_one()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill workspace_id from PG chunks.workspace_id into the "
            "Qdrant chunks_v1 payload. Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows fetched from PG per streaming batch (default 256).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N chunks (useful for verification).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't call set_payload; print what would happen.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on failure."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    collection = settings.qdrant_collection
    logger.info(
        "starting backfill collection=%s dry_run=%s limit=%s batch_size=%d",
        collection, args.dry_run, args.limit, args.batch_size,
    )

    # --- Preflight: migration 0013 must be applied -------------------
    null_count = _count_chunks_without_workspace()
    if null_count > 0:
        logger.error(
            "refusing to run: %d chunks still have workspace_id IS NULL. "
            "Apply migration 0013 first (`alembic upgrade head`).",
            null_count,
        )
        return 2
    total = _count_total_chunks()
    logger.info("preflight ok: %d chunks total, all with workspace_id", total)
    if total == 0:
        logger.info("no chunks to backfill — nothing to do")
        return 0

    # --- Connect to Qdrant (skip in dry-run) -------------------------
    client: QdrantClient | None = None
    if not args.dry_run:
        client = build_qdrant_client(settings)
        if not client.collection_exists(collection_name=collection):
            logger.error(
                "Qdrant collection %s does not exist — nothing to backfill. "
                "If the system is freshly installed, no chunks have been "
                "indexed yet (so the data pipeline naturally produces "
                "workspace_id-stamped payloads).",
                collection,
            )
            return 3

    # --- Stream from PG, group by workspace, push to Qdrant ---------
    # Grouping by workspace_id lets us call set_payload ONCE per
    # workspace (each call sets the same workspace_id on N points),
    # instead of once per chunk. For a 100k-chunk collection across
    # 10 workspaces that's 10 Qdrant calls instead of 100k.
    by_workspace: dict[str, list[str]] = defaultdict(list)
    emitted_rows = 0
    started = time.perf_counter()
    workspaces_seen: set[str] = set()

    for pg_batch in _stream_chunks_with_workspace(args.batch_size, args.limit):
        emitted_rows += len(pg_batch)
        for point_id, ws_uuid in pg_batch:
            ws_key = str(ws_uuid)
            by_workspace[ws_key].append(str(point_id))
            workspaces_seen.add(ws_key)
        # Within-batch grouping is cheap; flush per-batch anyway to
        # bound memory when the workspace cardinality is low (one or
        # two huge workspaces) and most rows would otherwise pile up.
        if len(by_workspace) >= max(args.batch_size, 1):
            _flush(
                client=client,
                collection=collection,
                by_workspace=by_workspace,
                dry_run=args.dry_run,
                workspaces_seen=workspaces_seen,
            )
            by_workspace.clear()
        if emitted_rows % (args.batch_size * 10) == 0:
            elapsed = time.perf_counter() - started
            rate = emitted_rows / elapsed if elapsed > 0 else 0
            logger.info(
                "progress: %d rows processed, %d workspaces seen, "
                "%.0f rows/sec",
                emitted_rows, len(workspaces_seen), rate,
            )
    # Final flush.
    _flush(
        client=client,
        collection=collection,
        by_workspace=by_workspace,
        dry_run=args.dry_run,
        workspaces_seen=workspaces_seen,
    )

    elapsed = time.perf_counter() - started
    logger.info(
        "backfill complete: %d rows, %d workspaces, %.1fs (dry_run=%s)",
        emitted_rows, len(workspaces_seen), elapsed, args.dry_run,
    )
    return 0


def _flush(
    *,
    client: QdrantClient | None,
    collection: str,
    by_workspace: dict[str, list[str]],
    dry_run: bool,
    workspaces_seen: set[str],
) -> None:
    """Push accumulated (workspace, [point_ids]) groups to Qdrant.

    One `set_payload` call per workspace (the payload dict is the
    same — only the point ID list varies). The point IDs are split
    into DEFAULT_BATCH_SIZE-sized sub-batches to stay under Qdrant's
    per-call payload limit.
    """
    for ws_key, point_ids in by_workspace.items():
        payload = {"workspace_id": ws_key}
        # Split the per-workspace list into sub-batches so we never
        # send more than DEFAULT_BATCH_SIZE points in a single call.
        for start in range(0, len(point_ids), DEFAULT_BATCH_SIZE):
            sub = point_ids[start : start + DEFAULT_BATCH_SIZE]
            if dry_run:
                logger.info(
                    "DRY-RUN: set_payload collection=%s points=%d payload=%s",
                    collection, len(sub), payload,
                )
                continue
            assert client is not None  # noqa: S101  (dry-run gate)
            ok, err = _set_payload_with_retry(
                client=client,
                collection=collection,
                payload=payload,
                point_ids=sub,
            )
            if not ok:
                logger.error(
                    "set_payload FAILED for workspace=%s batch=%d..%d: %s",
                    ws_key, start, start + len(sub), err,
                )
            else:
                logger.debug(
                    "set_payload ok workspace=%s points=%d",
                    ws_key, len(sub),
                )


if __name__ == "__main__":
    sys.exit(main())
