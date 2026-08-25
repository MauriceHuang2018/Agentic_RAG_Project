"""create sensitive_values table + import 14 default words (M6 decision #6)

Revision ID: 0005_sensitive_values
Revises: 0004_roles_workspace_scoped
Create Date: 2026-08-25

Creates the `sensitive_values` table that backs Page 15
(/admin/sensitive-info, system_admin only). The data-migration half
imports the 14 words from
`agentic_rag_project.post_processor.filter.DEFAULT_SENSITIVE_WORDS`
so admin starts with a populated preset list. `is_preset=TRUE` marks
these rows as system-provided — admin can toggle `is_active` per row
but cannot delete them (enforced in the FastAPI layer, not the DB).

TASK T1.3b / DESIGN §4.6.1.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_sensitive_values"
down_revision: str | Sequence[str] | None = "0004_roles_workspace_scoped"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 14 default words + category (mirrors DESIGN §2 Page 15 comments).
# Kept in sync with post_processor.filter.DEFAULT_SENSITIVE_WORDS.
DEFAULT_WORDS: tuple[tuple[str, str], ...] = (
    ("反动", "political"),
    ("色情", "porn"),
    ("暴力恐怖", "violence"),
    ("非法集资", "scam"),
    ("毒品", "other"),
    ("枪支", "other"),
    ("赌博", "other"),
    ("邪教", "other"),
    ("诈骗", "scam"),
    ("洗钱", "scam"),
    ("fuck", "profanity"),
    ("shit", "profanity"),
    ("asshole", "profanity"),
)


def upgrade() -> None:
    """Create sensitive_values + indexes, then seed 14 default rows."""
    op.create_table(
        "sensitive_values",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("word", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "category",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'custom'"),
        ),
        sa.Column(
            "is_preset",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "added_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_audit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_logs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Partial index on is_active — most queries filter on the active subset.
    op.create_index(
        "ix_sensitive_values_active",
        "sensitive_values",
        ["is_active"],
        postgresql_where=sa.text("is_active = TRUE"),
    )
    op.create_index(
        "ix_sensitive_values_category",
        "sensitive_values",
        ["category"],
    )

    # Data migration: import 14 default words as preset + active.
    sensitive_values = sa.table(
        "sensitive_values",
        sa.column("word", sa.String),
        sa.column("category", sa.String),
        sa.column("is_preset", sa.Boolean),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        sensitive_values,
        [
            {
                "word": word,
                "category": category,
                "is_preset": True,
                "is_active": True,
            }
            for word, category in DEFAULT_WORDS
        ],
    )


def downgrade() -> None:
    """Drop indexes first, then the table (cascade removes the 14 seeded rows)."""
    op.drop_index("ix_sensitive_values_category", table_name="sensitive_values")
    op.drop_index("ix_sensitive_values_active", table_name="sensitive_values")
    op.drop_table("sensitive_values")