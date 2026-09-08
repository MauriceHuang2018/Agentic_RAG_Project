"""feedback idempotency UNIQUE + audit_logs CHECK 扩 11 literals (2026-08-27)

Revision ID: 0012_feedback_idempotency_and_audit_expand
Revises: 0011_audit_logs_worm
Create Date: 2026-08-27

M5 (security hardening sprint) needs two related DB changes; bundling
them into one migration keeps the alembic chain linear and avoids
deploying two additive migrations back-to-back:

  1. `feedbacks` UNIQUE(message_id, user_id)
     F5 partial — POST /feedback is currently a free-replay; the same
     user can spam the same answer's feedback row. The fix is a
     partial idempotency contract: one feedback per (message, user),
     upsert behaviour so the user can correct their rating. Backfill
     is conservative: rows with the same (message_id, user_id) tuple
     are folded by `MAX(created_at)`; the older duplicates are
     archived into `feedback_dup_backup` (audit trail) before being
     deleted. The unique constraint is named `uq_feedbacks_message_user`
     and is the ON CONFLICT target for `repo.create_feedback` (T4).

  2. `audit_logs` CHECK constraint expand 8 → 11 literals
     Decision 3 (Align) and decision 7 (Consensus) added three new
     `AuditAction` members: `csat_read`, `role_bind`,
     `sensitive_word_update`. The DB-side whitelist must follow the
     Python Literal or every INSERT for the new actions will be
     rejected by `audit_logs_action_check`. The CHECK is rebuilt
     additively (no DROP TABLE, no data loss); the PL/pgSQL WORM
     trigger is unchanged (it does not branch on action literal).

Downgrade reverses both changes in opposite order. The `feedback_dup_backup`
rows are NOT restored to `feedbacks` (would re-introduce the duplicates
that step 1 was designed to remove); the backup table itself is kept
for post-incident inspection and dropped only by an explicit follow-up.

DESIGN reference: docs/m5_security/DESIGN_M5_security.md §4.1 + §5.1.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_feedback_idempotency_and_audit_expand"
down_revision: str | Sequence[str] | None = "0011_audit_logs_worm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Mirror of `audit.events.AuditAction` after M5 expansion (11 literals).
# Keep this list in lock-step with the Python Literal — every new value
# here is a contract change for the audit pipeline.
ALLOWED_ACTIONS_M5 = (
    "query",
    "ingest",
    "delete",
    "access_denied",
    "guardrail_block",
    "feedback_submit",
    "role_assign",
    "sensitive_update",
    "csat_read",
    "role_bind",
    "sensitive_word_update",
)


def upgrade() -> None:
    """Add feedback idempotency + expand audit_logs CHECK.

    Each step is idempotent (`IF NOT EXISTS` / `checkfirst=True`) so
    re-running on a partially-migrated DB is safe — same convention as
    migration 0011.
    """
    # --- 1. Backup table for duplicate-feedback rows (M5 backfill) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_dup_backup (
            id UUID PRIMARY KEY,
            message_id UUID NOT NULL,
            user_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            rating VARCHAR(16) NOT NULL,
            comment TEXT,
            ragas_scores JSONB,
            attribution_status VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            backed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            backup_reason TEXT NOT NULL DEFAULT '0012_unique_constraint_backfill'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_dup_backup_msg_user "
        "ON feedback_dup_backup (message_id, user_id)"
    )

    # --- 2. Backfill: fold duplicate (message_id, user_id) rows ---
    # Identify every (message_id, user_id) tuple with >1 row. For each,
    # keep the row with `MAX(created_at)` and archive the older ones.
    # The COPY-then-DELETE is done in one CTE per tuple via a single
    # INSERT...SELECT + DELETE statement pair below.
    op.execute(
        """
        WITH dup_groups AS (
            SELECT message_id, user_id, MAX(created_at) AS keep_ts
            FROM feedbacks
            GROUP BY message_id, user_id
            HAVING COUNT(*) > 1
        ),
        archived AS (
            INSERT INTO feedback_dup_backup (
                id, message_id, user_id, workspace_id, rating,
                comment, ragas_scores, attribution_status, created_at,
                backup_reason
            )
            SELECT f.id, f.message_id, f.user_id, f.workspace_id, f.rating,
                   f.comment, f.ragas_scores, f.attribution_status, f.created_at,
                   '0012_unique_constraint_backfill:older_of_duplicate'
            FROM feedbacks f
            JOIN dup_groups d
              ON f.message_id = d.message_id
             AND f.user_id = d.user_id
             AND f.created_at < d.keep_ts
            RETURNING id
        )
        DELETE FROM feedbacks f
        USING dup_groups d, archived a
        WHERE f.id = a.id
          AND f.message_id = d.message_id
          AND f.user_id = d.user_id
          AND f.created_at < d.keep_ts
        """
    )

    # --- 3. Add UNIQUE constraint (now safe — duplicates are gone) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_feedbacks_message_user'
                  AND conrelid = 'feedbacks'::regclass
            ) THEN
                ALTER TABLE feedbacks
                ADD CONSTRAINT uq_feedbacks_message_user
                UNIQUE (message_id, user_id);
            END IF;
        END
        $$;
        """
    )

    # --- 4. Expand audit_logs CHECK 8 → 11 literals ---
    # The 0011 migration built the original 8-literal CHECK. We DROP and
    # recreate with the M5-expanded list. The WORM trigger is unaffected
    # (it does not branch on action literal). The Python `AuditAction`
    # Literal in `audit.events.AuditAction` was updated in lock-step.
    op.execute(
        "ALTER TABLE audit_logs "
        "DROP CONSTRAINT IF EXISTS audit_logs_action_check"
    )
    actions_sql = "', '".join(ALLOWED_ACTIONS_M5)
    op.execute(
        f"ALTER TABLE audit_logs "
        f"ADD CONSTRAINT audit_logs_action_check "
        f"CHECK (action IN ('{actions_sql}'))"
    )


def downgrade() -> None:
    """Reverse: drop UNIQUE + restore old 8-literal CHECK.

    Note: `feedback_dup_backup` rows are NOT moved back into `feedbacks`
    — that would re-introduce the duplicates step 2 was designed to
    remove. The backup table is left in place for post-incident
    inspection; an explicit follow-up migration can drop it.
    """
    # --- 1. Restore audit_logs CHECK to the original 8-literal list ---
    op.execute(
        "ALTER TABLE audit_logs "
        "DROP CONSTRAINT IF EXISTS audit_logs_action_check"
    )
    original_8 = (
        "query", "ingest", "delete", "access_denied",
        "guardrail_block", "feedback_submit", "role_assign",
        "sensitive_update",
    )
    actions_sql = "', '".join(original_8)
    op.execute(
        f"ALTER TABLE audit_logs "
        f"ADD CONSTRAINT audit_logs_action_check "
        f"CHECK (action IN ('{actions_sql}'))"
    )

    # --- 2. Drop UNIQUE constraint ---
    op.execute(
        "ALTER TABLE feedbacks "
        "DROP CONSTRAINT IF EXISTS uq_feedbacks_message_user"
    )

    # Intentionally NOT dropping feedback_dup_backup — see module docstring.