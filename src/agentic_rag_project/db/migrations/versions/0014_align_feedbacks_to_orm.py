"""align feedbacks to current ORM (workspace_id + rating + ragas + status) — 2026-09-07

Revision ID: 0014_align_feedbacks_to_orm
Revises: 0013_chunks_workspace_id
Create Date: 2026-09-07

CLOSES a schema drift that crashed POST /api/v1/feedback with HTTP 500
(user reported 2026-09-07 via FeedbackModal screenshot — frontend
showed "网络错误，请稍后重试" twice). Root cause chain:

    Feedback ORM (`feedback/models.py:154-198`) declares 5 columns that
    `feedbacks` table never had:

      * `workspace_id`     UUID NOT NULL        ← missing
      * `rating`           VARCHAR(16) NOT NULL ← missing
      * `ragas_scores`     JSONB                ← missing
      * `attribution_status` VARCHAR(16) NOT NULL ← missing

    The original 0001 migration only created `score INTEGER NOT NULL`
    (a column the ORM no longer reads). Every POST /feedback tried
    INSERT into a column that doesn't exist → psycopg.errors.UndefinedColumn
    → FastAPI returned 500. The frontend (commit d34ef5e) now passes
    the *real* error through; this migration fixes the DB so the
    INSERT succeeds.

What this migration does (each step idempotent where possible):

    1. ADD COLUMN `workspace_id` UUID (nullable, FK → workspaces.id
       ON DELETE CASCADE) — mirrors chunks.workspace_id FK (0013).
    2. Backfill `workspace_id` from `messages.conversation_id` →
       `conversations.workspace_id`. Three-table join is the only
       chain since `feedbacks.message_id` references `messages.id`.
       For our empty prod table (count=0 verified 2026-09-07) the
       UPDATE is a no-op; the join is kept so re-running against a
       populated DB is safe.
    3. SET NOT NULL on `workspace_id` (after backfill). Per user
       authorization "加 workspace_id NOT NULL" 2026-09-07.
    4. CREATE INDEX `ix_feedbacks_workspace_id` (b-tree; supports
       `WHERE workspace_id = ANY(...)` lookups + composite index 5).
    5. ADD COLUMN `rating` VARCHAR(16) NOT NULL DEFAULT 'like'.
       Replaces the legacy `score INTEGER` column. We default to
       `'like'` because the legacy `score=1` in old rows meant
       thumbs-up; the table is empty so the default never applies
       to real data.
    6. ADD COLUMN `ragas_scores` JSONB (nullable).
    7. ADD COLUMN `attribution_status` VARCHAR(16) NOT NULL
       DEFAULT 'pending'. Mirrors 0012 `feedback_dup_backup` schema
       (VARCHAR not PG enum) for cross-table consistency.
    8. DROP COLUMN `score` — ORM no longer reads it; legacy column
       becomes dead weight and would block future migrations.
    9. CREATE COMPOSITE INDEX `ix_feedbacks_workspace_rating_created`
       on (workspace_id, rating, created_at). Pinned by ORM
       `__table_args__` (models.py:298-303) and drives the CSAT
       dashboard's "feedback for workspace X by rating over time"
       query (M4.4 csat_queries.py). Without this index the
       dashboard hits a seq scan.

What we intentionally do NOT do:

    * `reason_tag_id` / `category_id` / `auto_categorized` are kept.
      The ORM doesn't declare them, but no admin has asked to drop
      them and they're harmless dead columns (same convention as
      0010's keep-`label`-for-back-compat).
    * `comment` stays NOT NULL DEFAULT '' (0001 default; ORM
      declares nullable=True but PG-side NOT NULL doesn't bite us
      because we never INSERT without a value).
    * The M5 FK to `feedback_categories.id` is preserved on the
      legacy `category_id` column — admins use it for the manual
      triage workflow.

Downgrade reverses in opposite order: drop composite index, drop
new columns, drop NOT NULL on workspace_id, drop FK + index, drop
column. The legacy `score` column is recreated as INTEGER NOT NULL
so a downgrade preserves the 0001 schema exactly.

DESIGN reference: docs/feedback_schema_drift/SPEC_feedback-schema-drift.md
(2026-09-07, retro-fitted after this fix).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_align_feedbacks_to_orm"
down_revision: str | Sequence[str] | None = "0013_chunks_workspace_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Bring `feedbacks` table to current ORM schema.

    Idempotent: each step wrapped in IF NOT EXISTS / DO blocks so
    re-running on a partially-migrated DB is safe (same convention as
    0010 / 0011 / 0012 / 0013).
    """
    # --- 1. ADD COLUMN workspace_id (nullable first) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'workspace_id'
            ) THEN
                ALTER TABLE feedbacks
                ADD COLUMN workspace_id UUID
                REFERENCES workspaces(id) ON DELETE CASCADE;
            END IF;
        END
        $$;
        """
    )

    # --- 2. Backfill workspace_id from messages → conversations chain ---
    # feedbacks.message_id → messages.id → messages.conversation_id →
    # conversations.id → conversations.workspace_id. Three-table join
    # is the only chain that exists (verified via information_schema
    # 2026-09-07). For the empty prod table (count=0) this is a
    # no-op UPDATE; the join is kept so the migration is correct on
    # any populated DB. LEFT JOIN surfaces orphan rows for the
    # sanity check below.
    op.execute(
        """
        UPDATE feedbacks f
        SET workspace_id = c.workspace_id
        FROM messages m, conversations c
        WHERE f.message_id = m.id
          AND m.conversation_id = c.id
          AND f.workspace_id IS NULL
        """
    )

    # --- 2b. Sanity check: any feedback still NULL after backfill? ---
    # Should be impossible per the JOIN above (every message has a
    # conversation; every conversation has a workspace_id since 0001),
    # but we don't want to silently SET NOT NULL on a table with
    # orphan rows. Raising here is fail-fast.
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM feedbacks WHERE workspace_id IS NULL")
    ).scalar_one()
    if null_count > 0:
        raise RuntimeError(
            f"0014_align_feedbacks_to_orm backfill left {null_count} "
            f"feedbacks rows with NULL workspace_id — investigate orphan "
            f"feedbacks (message_id with no conversation, or conversation "
            f"with no workspace_id) before re-running this migration."
        )

    # --- 3. SET NOT NULL on workspace_id ---
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'workspace_id'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE feedbacks
                ALTER COLUMN workspace_id SET NOT NULL;
            END IF;
        END
        $$;
        """
    )

    # --- 4. CREATE INDEX ix_feedbacks_workspace_id ---
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedbacks_workspace_id "
        "ON feedbacks (workspace_id)"
    )

    # --- 5. ADD COLUMN rating VARCHAR(16) NOT NULL DEFAULT 'like' ---
    # Replaces the legacy `score` Int column. Default 'like' is safe
    # because: (a) prod table is empty (verified count=0 2026-09-07),
    # (b) legacy score=1 was thumbs-up semantics. New rows will
    # always pass an explicit rating from POST /feedback body, so the
    # default is just a safety net.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'rating'
            ) THEN
                ALTER TABLE feedbacks
                ADD COLUMN rating VARCHAR(16) NOT NULL DEFAULT 'like';
            END IF;
        END
        $$;
        """
    )

    # --- 6. ADD COLUMN ragas_scores JSONB (nullable) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'ragas_scores'
            ) THEN
                ALTER TABLE feedbacks
                ADD COLUMN ragas_scores JSONB;
            END IF;
        END
        $$;
        """
    )

    # --- 7. ADD COLUMN attribution_status VARCHAR(16) NOT NULL DEFAULT 'pending' ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'attribution_status'
            ) THEN
                ALTER TABLE feedbacks
                ADD COLUMN attribution_status VARCHAR(16) NOT NULL
                DEFAULT 'pending';
            END IF;
        END
        $$;
        """
    )

    # --- 8. DROP COLUMN score (replaced by rating) ---
    # ORM no longer reads `score`; keeping it would block future
    # migrations and confuse new contributors. No rows exist so this
    # is metadata-only.
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS score")

    # --- 9. CREATE COMPOSITE INDEX (workspace_id, rating, created_at) ---
    # Pinned by `Index("ix_feedbacks_workspace_rating_created", ...)`
    # in feedback/models.py:298-303. Drives the CSAT dashboard
    # "feedback for workspace X by rating over time" query.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedbacks_workspace_rating_created "
        "ON feedbacks (workspace_id, rating, created_at)"
    )


def downgrade() -> None:
    """Reverse: drop composite index, drop new columns, drop NOT NULL,
    drop FK + index, drop workspace_id column.

    The legacy `score` column is RECREATED so downgrade preserves
    the 0001 schema shape exactly. (We don't try to round-trip any
    data — the prod table was empty at upgrade time.)
    """
    # --- 1. Drop composite index ---
    op.execute("DROP INDEX IF EXISTS ix_feedbacks_workspace_rating_created")

    # --- 2. Drop new ORM-declared columns ---
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS attribution_status")
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS ragas_scores")
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS rating")

    # --- 3. Restore legacy score column (so downgrade matches 0001 shape) ---
    op.execute(
        "ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 1"
    )

    # --- 4. Drop NOT NULL on workspace_id ---
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'workspace_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE feedbacks
                ALTER COLUMN workspace_id DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )

    # --- 5. Drop index ---
    op.execute("DROP INDEX IF EXISTS ix_feedbacks_workspace_id")

    # --- 6. Drop workspace_id column (FK + values go with it) ---
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS workspace_id")


__all__: list[str] = []  # alembic only uses the module-level names above