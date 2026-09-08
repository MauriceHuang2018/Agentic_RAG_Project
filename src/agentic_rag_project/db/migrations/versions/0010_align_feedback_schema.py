"""align feedback_categories to current ORM + create ticket_statuses (2026-08-25)

Revision ID: 0010_align_feedback_schema
Revises: 0009_users_password_reset_audit
Create Date: 2026-08-25

CLOSES a schema drift surfaced during M2 E2E:

  * `feedback_categories` was created by 0001 with columns
    `label, auto_classify_prompt, is_active, sort_order`. The ORM
    (`feedback/models.py:79-91`) was later rewritten to read
    `name_zh, description, is_system` but no migration followed.
    Tests didn't notice because they use a fresh schema session
    (Base.metadata.create_all), not the alembic chain.

  * `ticket_statuses` was never created at all — only the ORM
    (`feedback/models.py:126-144`) and downstream repository endpoints
    (e.g. `GET /feedback/ticket-statuses`). Any fresh PG + alembic
    head would have crashed the first time that endpoint ran.

This migration does three things, in order:

  1. `feedback_categories`: add `name_zh` / `description` / `is_system`
     as nullable columns, backfill from existing `label` (and safe
     defaults), then `SET NOT NULL`. The legacy columns
     (`auto_classify_prompt`, `is_active`, `sort_order`) are kept
     in place to avoid breaking any external report / dashboard
     that might still read them — they're harmless dead columns
     once the ORM stops touching them.

  2. `ticket_statuses`: CREATE TABLE matching the ORM exactly
     (id UUID PK default gen_random_uuid, key VARCHAR(64) UNIQUE,
     name_zh VARCHAR(128), description TEXT NULL, color VARCHAR(32) NULL,
     is_terminal / is_system BOOL NOT NULL default false,
     display_order INT NOT NULL default 0, created_at TIMESTAMPTZ).

  3. Downgrade drops the new columns / table cleanly.

DESIGN reference: §5.4 feedback / ticket dictionary schema.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_align_feedback_schema"
down_revision: str | Sequence[str] | None = "0009_users_password_reset_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Bring `feedback_categories` to current ORM + create `ticket_statuses`."""
    # --- 1. feedback_categories: backfill + align ---
    op.add_column(
        "feedback_categories",
        sa.Column("name_zh", sa.String(128), nullable=True),
    )
    op.add_column(
        "feedback_categories",
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.add_column(
        "feedback_categories",
        sa.Column("is_system", sa.Boolean(), nullable=True),
    )
    # Backfill from legacy `label` (every existing row has it).
    # For brand-new empty DBs the UPDATE is a no-op (no rows).
    op.execute(
        "UPDATE feedback_categories "
        "SET name_zh = label, "
        "    description = COALESCE(description, ''), "
        "    is_system = COALESCE(is_system, FALSE) "
        "WHERE name_zh IS NULL"
    )
    # Tighten NOT NULL now that backfill is done.
    op.alter_column("feedback_categories", "name_zh", nullable=False)
    op.alter_column("feedback_categories", "is_system", nullable=False)

    # --- 2. ticket_statuses: full table from ORM ---
    op.create_table(
        "ticket_statuses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name_zh", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(32), nullable=True),
        sa.Column(
            "is_terminal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Reverse: drop new columns / new table.

    Note: legacy columns (`label`, `auto_classify_prompt`, `is_active`,
    `sort_order`) are intentionally NOT touched — they pre-date this
    migration and any data still using them belongs to a separate
    cleanup task.
    """
    op.drop_table("ticket_statuses")
    op.drop_column("feedback_categories", "is_system")
    op.drop_column("feedback_categories", "description")
    op.drop_column("feedback_categories", "name_zh")