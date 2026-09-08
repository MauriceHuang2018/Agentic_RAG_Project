"""feedbacks.comment NULL — closing the second half of the 0014 drift (2026-09-07)

Revision ID: 0015_feedbacks_comment_nullable
Revises: 0014_align_feedbacks_to_orm
Create Date: 2026-09-07

CLOSES the `comment` half of the schema drift surfaced during the
FeedbackModal bug investigation. The 0014 migration added the ORM's
five missing columns (`workspace_id`, `rating`, `ragas_scores`,
`attribution_status`) but missed `comment`, which the ORM declares as
`nullable=True` while the DB has kept the 0001 NOT NULL DEFAULT ''
shape. The first curl probe after 0014 surfaced this as
`psycopg.errors.NotNullViolation: null value in column "comment" of
relation "feedbacks"` — same family of bug as the original 500.

Why a separate migration instead of amending 0014:

    0014 has already been applied to the prod DB (alembic_version
    table holds `0014_align_feedbacks_to_orm`). Re-running it would
    require a `downgrade` + `upgrade` round-trip that does no real
    work and risks operator confusion. The alembic chain convention
    is "one revision per forward step" — adding 0015 is the
    honest reflection of "we discovered a second drift after 0014
    landed".

What this migration does:

    1. ALTER COLUMN `comment` DROP NOT NULL (idempotent via DO block
       guard on is_nullable). The legacy DEFAULT '' is kept so
       existing INSERT paths that omit `comment` still succeed (PG
       fills ''), but the constraint is no longer enforced — matches
       the ORM `Mapped[str | None]` declaration.

What we intentionally do NOT do:

    * Migrate any existing `''` strings to NULL — table is empty
      (verified count=0 2026-09-07), and the legacy empty default
      is semantically equivalent to NULL for the comment field.
    * Drop the DEFAULT '' — keeping it is a no-cost safety net for
      any future raw INSERT path that forgets to set comment.

DESIGN reference: docs/feedback_schema_drift/SPEC_feedback-schema-drift.md
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_feedbacks_comment_nullable"
down_revision: str | Sequence[str] | None = "0014_align_feedbacks_to_orm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop NOT NULL on feedbacks.comment to match ORM `nullable=True`."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'comment'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE feedbacks ALTER COLUMN comment DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Reverse: re-add NOT NULL DEFAULT '' (matches 0001 shape).

    Note: re-adding NOT NULL on a column that may now contain NULLs
    will fail until those rows are cleaned. Empty prod table
    (verified count=0) means this downgrade is metadata-only.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'feedbacks'
                  AND column_name = 'comment'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE feedbacks
                ALTER COLUMN comment SET NOT NULL,
                ALTER COLUMN comment SET DEFAULT '';
            END IF;
        END
        $$;
        """
    )


__all__: list[str] = []  # alembic only uses the module-level names above