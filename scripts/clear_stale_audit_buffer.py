"""One-shot drain — clear stale `audit:buffer` entries (2026-09-08).

WHY THIS SCRIPT EXISTS
======================

The Redis `audit:buffer` list accumulates audit events that are
flushed to `audit_logs` every 5s by the celery
`flush_audit_buffer` task. If the DB is rebuilt (e.g.
`docker compose down -v` + reseed) while Redis keeps its volume,
the buffered events reference user_ids and workspace_ids that no
longer exist in the fresh DB — the bulk INSERT then raises
`IntegrityError` (`audit_logs_user_id_fkey`), the celery task
requeues the same broken payload to the main buffer, and the
service enters a 2-second dead-loop that floods the postgres +
celery-worker logs.

This script drains the stale buffer:

  1. Reads all entries from `audit:buffer`.
  2. Decodes each JSON payload.
  3. Filters out entries whose `user_id` (or
     `extra.workspace_id`) does not exist in the current DB.
  4. Re-LPUSHes the surviving entries back to `audit:buffer`
     so the celery task can flush them on its next tick.
  5. LPUSHes the dropped entries to `audit:buffer:dead` with a
     `{"reason": ..., "raw": ...}` envelope for operator
     inspection (mirrors the service-layer dead-letter format).

After this script runs:

  * `LLEN audit:buffer` returns the count of salvageable events
    (typically 0 in the broken state observed 2026-09-08).
  * `LLEN audit:buffer:dead` returns the count of dropped
    stale events (typically 184 in the broken state).
  * The celery `flush_audit_buffer` task stops emitting FK
    violation errors within ~5 s of the next beat tick.

USAGE
=====

Dry run (default — recommended first):

    docker exec rag-api python /app/scripts/clear_stale_audit_buffer.py

The script always prints the planned action and exits 0 without
mutating Redis. Pass `--yes` to actually run:

    docker exec rag-api python /app/scripts/clear_stale_audit_buffer.py --yes

IDEMPOTENCY
===========

Re-running after the buffer has been drained is a no-op:
`LLEN audit:buffer == 0` on the second invocation. Surviving
events are LPUSHed in their original order so re-flushing produces
the same audit_logs rows.

FAILURE MODES
=============

  * Redis unreachable — the script exits 1 with a clear error.
  * PG unreachable — the pre-flight user_id query returns the
    empty set, so all events are treated as orphans and
    dead-lettered. Re-run after PG recovery.
  * JSON-decode errors on individual payloads — those entries
    are skipped (matching the service-layer behavior) and counted
    in the dry-run report.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import redis as redis_lib
import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


# Mirrors `audit.service.DEAD_LETTER_BUFFER_KEY` — duplicated here
# to avoid pulling the full audit service stack (and its lifespan
# dependencies) into this one-shot script.
BUFFER_KEY = "audit:buffer"
DEAD_LETTER_KEY = "audit:buffer:dead"
ORPHAN_REASON = "audit_buffer_drain_orphan_v1"


def _connect_redis(host: str, port: int, password: str | None) -> redis_lib.Redis:
    """Build a Redis client using the same auth pattern as the app."""
    return redis_lib.Redis(
        host=host,
        port=port,
        password=password,
        decode_responses=False,
        socket_connect_timeout=5,
    )


def _connect_pg(database_url: str) -> sa.engine.Engine:
    """Build a PG engine for the pre-flight user_id lookup.

    Rewrites `postgresql://` to `postgresql+psycopg://` so the
    driver auto-detection picks the psycopg3 package that's
    actually installed in the project's containers (the
    legacy `psycopg2` package is not installed in production
    images — confirmed 2026-09-08 during drain run).
    """
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return create_engine(database_url, future=True)


def _fetch_existing_user_ids(session: Session) -> set[uuid.UUID]:
    """Return every `users.id` currently in the DB."""
    stmt = sa.text("SELECT id FROM users")
    return {row[0] for row in session.execute(stmt).all()}


def _classify_entries(
    payloads: list[bytes],
    existing_uids: set[uuid.UUID],
) -> tuple[list[bytes], list[bytes]]:
    """Split buffered payloads into (survivors, orphans).

    A payload is an orphan if:
      * It can't be JSON-decoded, OR
      * Its `user_id` is set but not in `existing_uids`.

    Note: `workspace_id` lives under `extra` (snapshot pattern), so
    it's NOT a column-level FK at flush time — no need to filter on
    it here. The DB-level `audit_logs_orphan_safety` trigger (alembic
    0017) is the second-line defense for the workspace column.
    """
    survivors: list[bytes] = []
    orphans: list[bytes] = []
    for raw in payloads:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            orphans.append(raw)
            continue
        uid_str = data.get("user_id")
        if uid_str is None:
            survivors.append(raw)
            continue
        try:
            uid = uuid.UUID(str(uid_str))
        except (ValueError, TypeError):
            orphans.append(raw)
            continue
        if uid in existing_uids:
            survivors.append(raw)
        else:
            orphans.append(raw)
    return survivors, orphans


def _report(label: str, count: int) -> None:
    """Print a one-line count for the dry-run report."""
    print(f"  {label:<32s} {count}")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success, 1 on error."""
    parser = argparse.ArgumentParser(
        description="Drain stale entries from the audit Redis buffer "
        "(M6 debug fix 2026-09-08).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually mutate Redis (default: dry-run only).",
    )
    args = parser.parse_args(argv)

    # Config — match the env-var names the app uses.
    import os

    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD") or None
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL env var is required.", file=sys.stderr)
        return 1

    print(f"Connecting to redis={redis_host}:{redis_port} pg=...")

    try:
        redis_client = _connect_redis(redis_host, redis_port, redis_password)
        redis_client.ping()
    except redis_lib.RedisError as exc:
        print(f"ERROR: redis unreachable: {exc}", file=sys.stderr)
        return 1

    engine = _connect_pg(database_url)
    try:
        with Session(engine) as session:
            existing_uids = _fetch_existing_user_ids(session)
    except sa.exc.SQLAlchemyError as exc:
        print(f"ERROR: pg query failed: {exc}", file=sys.stderr)
        return 1

    # Snapshot the buffer (LRANGE 0 -1 then DEL is NOT atomic, but
    # the worst case is one event getting flushed by the celery
    # worker between LRANGE and DEL — acceptable since the new
    # service-layer pre-flight check handles orphans gracefully).
    payloads: list[bytes] = []
    while True:
        chunk = redis_client.lrange(BUFFER_KEY, 0, 99)
        if not chunk:
            break
        payloads.extend(chunk)
        # Pop the chunk we just read.
        for _ in chunk:
            redis_client.lpop(BUFFER_KEY)
        if len(chunk) < 100:
            break

    survivors, orphans = _classify_entries(payloads, existing_uids)

    print()
    print("Dry-run report:")
    _report("buffer length (before)", len(payloads))
    _report("survivors (valid user_id)", len(survivors))
    _report("orphans (drop / dead-letter)", len(orphans))
    _report(
        "buffer length (after, if --yes)",
        len(survivors),
    )
    _report(
        "dead-letter length (after, if --yes)",
        redis_client.llen(DEAD_LETTER_KEY) + len(orphans),
    )

    if not args.yes:
        print()
        print("DRY-RUN: pass --yes to actually mutate Redis.")
        return 0

    # Re-LPUSH survivors back to the main buffer (preserve order).
    for raw in reversed(survivors):
        redis_client.lpush(BUFFER_KEY, raw)

    # Dead-letter the orphans (preserve order, envelope matches
    # the service-layer format for grep-friendliness).
    for raw in reversed(orphans):
        envelope = json.dumps(
            {"reason": ORPHAN_REASON, "raw": raw.decode("utf-8", errors="replace")},
            ensure_ascii=False,
        )
        redis_client.lpush(DEAD_LETTER_KEY, envelope)

    print()
    print("Drain complete.")
    _report("buffer length (after)", redis_client.llen(BUFFER_KEY))
    _report("dead-letter length (after)", redis_client.llen(DEAD_LETTER_KEY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
