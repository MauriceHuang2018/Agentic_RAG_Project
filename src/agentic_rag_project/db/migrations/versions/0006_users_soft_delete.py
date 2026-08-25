"""add users.deleted_at for soft delete (M6 decision #9)

Revision ID: 0006_users_soft_delete
Revises: 0005_sensitive_values
Create Date: 2026-08-25

Adds `users.deleted_at TIMESTAMPTZ NULL` plus a partial index on
active users (deleted_at IS NULL). All user-facing queries must
filter `WHERE deleted_at IS NULL` from now on; the audit_logs FK
on user_id uses `ON DELETE SET NULL` so historical rows survive.

TASK T1.3c / DESIGN §4.6.2.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_users_soft_delete"
down_revision: str | Sequence[str] | None = "0005_sensitive_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable deleted_at + partial index on active users."""
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — speeds up the "list active users" query that
    # every RBAC path runs. SQLite doesn't support partial indexes,
    # so we drop the postgresql_where clause there.
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.create_index(
            "ix_users_active",
            "users",
            ["deleted_at"],
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    else:
        # SQLite test path: a plain index is the closest portable
        # equivalent — the production DDL above is what ships.
        op.create_index("ix_users_active", "users", ["deleted_at"])


def downgrade() -> None:
    """Reverse: drop the index, then the column."""
    op.drop_index("ix_users_active", table_name="users")
    op.drop_column("users", "deleted_at")