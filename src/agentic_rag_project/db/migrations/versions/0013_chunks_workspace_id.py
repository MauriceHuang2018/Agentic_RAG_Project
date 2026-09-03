"""chunks.workspace_id NOT NULL — P0 data pipeline fix (2026-09-03)

Revision ID: 0013_chunks_workspace_id
Revises: 0012_feedback_idempotency_and_audit_expand
Create Date: 2026-09-03

DESIGN docs/workspace_id_pipeline/DESIGN §2.1 — three-step migration:
  1. ADD COLUMN workspace_id (nullable so existing rows survive)
  2. CREATE INDEX ix_chunks_workspace_id
  3. UPDATE chunks SET workspace_id = documents.workspace_id (backfill)
  4. ALTER COLUMN ... SET NOT NULL

Why this migration exists (root cause analysis on 2026-09-03):

  The `chunks` table was designed before the M5 RBAC refactor
  introduced workspace_id-scoped permissions. Consequently:

    a) Qdrant chunks written by `doc_processor/indexer._build_point`
       had no `workspace_id` in their payload (verified by
       POST /collections/chunks_v1/points/scroll — 100 sampled
       points returned payload keys `['chunk_id','document_id',
       'parent_id','content','block_kind','page']`, no workspace_id).

    b) `acl_filter.build_user_filter` (claimed in T5.3 docstring)
       was never implemented, so the chat path sent no qdrant_filter
       to HybridSearcher.

  Combined, the retriever returned 0 hits → synthesizer emitted an
  empty answer.

  This migration closes half the gap (PG side). The Qdrant payload
  side is closed by:

    - the data-pipeline commits in this PR (Pydantic + chunker +
      embedder + indexer carry workspace_id);
    - the operational backfill script
      `scripts/backfill_chunk_workspace_id.py` (run once after this
      migration to populate Qdrant payloads for chunks already
      written before this change).

Migration ordering matters:

  We add the column as NULLABLE so existing rows survive the
  ADD COLUMN (PG 16 is fast on ADD COLUMN without default, but
  we still want the UPDATE backfill to complete before we tighten
  to NOT NULL — otherwise the migration aborts on the first orphan
  row). The NOT NULL constraint is added AFTER the UPDATE so the
  alembic chain is atomic from the application's perspective: any
  chunk row that exists post-migration is guaranteed to carry a
  workspace_id.

FK choice:

  workspace_id → workspaces.id ON DELETE CASCADE (matches the
  documents.workspace_id FK and the user_roles.workspace_id FK in
  migration 0001; deleting a workspace drops its chunks).

Downgrade reverses in opposite order: DROP NOT NULL would be the
reverse of the original SET NOT NULL, then the UPDATE is dropped
(de-facto inverted: backfill is conservative — leaving the value
in place — see downgrade comment).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_chunks_workspace_id"
down_revision: str | Sequence[str] | None = "0012_feedback_idempotency_and_audit_expand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add workspace_id to chunks, backfill from documents, set NOT NULL.

    Idempotent where possible (IF NOT EXISTS on index, checkfirst on
    the FK + NOT NULL) — re-running on a partially-migrated DB is
    safe, same convention as 0010 / 0011 / 0012.
    """
    # --- 1. ADD COLUMN workspace_id (nullable) ---
    # IF NOT EXISTS so re-runs against a partially-applied migration
    # don't fail. The COLUMN type mirrors documents.workspace_id:
    # UUID + FK to workspaces(id) ON DELETE CASCADE.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'chunks'
                  AND column_name = 'workspace_id'
            ) THEN
                ALTER TABLE chunks
                ADD COLUMN workspace_id UUID
                REFERENCES workspaces(id) ON DELETE CASCADE;
            END IF;
        END
        $$;
        """
    )

    # --- 2. CREATE INDEX ix_chunks_workspace_id ---
    # b-tree is the default; supports the workspace-scoped lookups
    # the chat ACL filter needs (e.g. `WHERE workspace_id = ANY(...)`).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_workspace_id "
        "ON chunks (workspace_id)"
    )

    # --- 3. Backfill workspace_id from documents ---
    # Every chunk row references a document row; every document row
    # has workspace_id (NOT NULL, since migration 0001). The JOIN
    # therefore always finds a value — unless a chunk references a
    # document that was soft-deleted (documents.deleted_at IS NOT
    # NULL) or hard-deleted (which is unusual because CASCADE on
    # documents.workspace_id cascades chunks too). The LEFT JOIN
    # surfaces any orphans so the assertion below catches them.
    op.execute(
        """
        UPDATE chunks c
        SET workspace_id = d.workspace_id
        FROM documents d
        WHERE c.document_id = d.id
          AND c.workspace_id IS NULL
        """
    )

    # --- 3b. Sanity check: any chunk still NULL after backfill? ---
    # Should be impossible per the JOIN above (every document has a
    # workspace_id), but we don't want to silently SET NOT NULL on a
    # table with orphan rows. Raising here is fail-fast — operator
    # can investigate the orphan chunk and either restore the parent
    # document or set a manual workspace_id, then re-run.
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM chunks WHERE workspace_id IS NULL")
    ).scalar_one()
    if null_count > 0:
        raise RuntimeError(
            f"0013_chunks_workspace_id backfill left {null_count} chunks "
            f"with NULL workspace_id — investigate orphan chunks before "
            f"re-running this migration."
        )

    # --- 4. SET NOT NULL ---
    # Now safe: every chunk row has a value. CHECK constraint form
    # (instead of ALTER COLUMN ... SET NOT NULL) is no-op-safe on
    # re-run (DO block guards).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'chunks'
                  AND column_name = 'workspace_id'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE chunks
                ALTER COLUMN workspace_id SET NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Reverse: drop NOT NULL, drop index, drop column.

    The backfill UPDATE is NOT reversed — the values are kept on
    downgrade. Rationale: dropping the column loses data; if the
    operator wants to truly roll back, they should restore from a
    backup. We drop the NOT NULL constraint first so that, in
    theory, the column could be kept while keeping the migration
    chain consistent (a future migration could rebuild the
    workspace_id scoping without re-adding the column).
    """
    # --- 1. Drop NOT NULL ---
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'chunks'
                  AND column_name = 'workspace_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE chunks
                ALTER COLUMN workspace_id DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )

    # --- 2. Drop index ---
    op.execute("DROP INDEX IF EXISTS ix_chunks_workspace_id")

    # --- 3. Drop column (FK + values go with it) ---
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS workspace_id")
