"""CREATE feedback_tickets + feedback_attributions — third half of feedback drift (2026-09-07)

Revision ID: 0016_create_feedback_dependent_tables
Revises: 0015_feedbacks_comment_nullable
Create Date: 2026-09-07

CLOSES the third drift surfaced during FeedbackModal debugging. The
ORM declares THREE feedback-related tables; only ONE was in the DB:

    ✓ `feedbacks`            (0001 — but missing 5 columns, fixed 0014)
    ✓ `feedback_categories`  (0001 — aligned 0010)
    ✓ `feedback_tags`        (0001)
    ✗ `feedback_tickets`     (ORM `feedback/models.py:249-293`)
    ✗ `feedback_attributions`(ORM `feedback/models.py:214-246`)

Same shape as the `ticket_statuses` drift closed by migration 0010:
the ORM was updated to add ticket + attribution rows, the schema was
never migrated. POST /feedback gets past the INSERT into `feedbacks`
(after 0014 + 0015) and dies on the very next call:

    repository.create_ticket()
        → INSERT INTO feedback_tickets → UndefinedTable

What this migration does:

    1. CREATE TABLE feedback_attributions matching ORM exactly:
         id UUID PK default gen_random_uuid(),
         feedback_id UUID NOT NULL FK→feedbacks.id ON DELETE CASCADE,
         category_id UUID NOT NULL FK→feedback_categories.id,
         confidence FLOAT NOT NULL default 1.0,
         reasoning TEXT NULL,
         matched_rule VARCHAR(128) NULL,
         created_at TIMESTAMPTZ NOT NULL default now().
       Index on feedback_id for the dashboard / M4.4 csat_queries path.

    2. CREATE TABLE feedback_tickets matching ORM exactly:
         id UUID PK default gen_random_uuid(),
         feedback_id UUID NOT NULL UNIQUE FK→feedbacks.id ON DELETE CASCADE,
         status VARCHAR(64) NOT NULL default 'collected' FK→ticket_statuses.key
           ON DELETE RESTRICT,
         assigned_to UUID NULL,
         note TEXT NULL,
         created_at + updated_at TIMESTAMPTZ NOT NULL default now().
       Index on feedback_id (covered by UNIQUE).

Why a separate migration instead of amending 0014 or 0015:

    Both 0014 and 0015 have been applied to the prod DB. Re-running
    them requires a downgrade+upgrade round-trip that does no real
    work and risks operator confusion. The alembic chain convention
    is "one revision per forward step". 0016 is the honest record
    that we discovered a third drift after 0015 landed.

What we intentionally do NOT do:

    * Backfill any data into the new tables — both are empty by
      construction (feedback_tickets is created per feedback row;
      feedback_attributions is created per successful attribution).
    * Touch the legacy `feedbacks.reason_tag_id` / `category_id`
      columns — those are preserved by 0014's no-op intent and any
      cleanup belongs to a separate admin-task migration.

DESIGN reference: docs/feedback_schema_drift/SPEC_feedback-schema-drift.md
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_create_feedback_dependent_tables"
down_revision: str | Sequence[str] | None = "0015_feedbacks_comment_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two ORM-declared tables that 0001 omitted."""
    # --- 1. feedback_attributions ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_attributions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            feedback_id UUID NOT NULL
                REFERENCES feedbacks(id) ON DELETE CASCADE,
            category_id UUID NOT NULL
                REFERENCES feedback_categories(id),
            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            reasoning TEXT,
            matched_rule VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_attributions_feedback_id "
        "ON feedback_attributions (feedback_id)"
    )

    # --- 2. feedback_tickets ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_tickets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            feedback_id UUID NOT NULL UNIQUE
                REFERENCES feedbacks(id) ON DELETE CASCADE,
            status VARCHAR(64) NOT NULL DEFAULT 'collected'
                REFERENCES ticket_statuses(key) ON DELETE RESTRICT,
            assigned_to UUID,
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # feedback_id is already UNIQUE → index already covers lookups by
    # feedback_id. No additional index needed.


def downgrade() -> None:
    """Reverse: drop the two tables."""
    op.execute("DROP TABLE IF EXISTS feedback_tickets")
    op.execute("DROP TABLE IF EXISTS feedback_attributions")


__all__: list[str] = []  # alembic only uses the module-level names above