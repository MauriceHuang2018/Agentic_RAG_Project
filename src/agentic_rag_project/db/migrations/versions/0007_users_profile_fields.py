"""add users.display_name + users.avatar_url (M6 Page 14 self-service)

Revision ID: 0007_users_profile_fields
Revises: 0006_users_soft_delete
Create Date: 2026-08-25

Adds two nullable profile fields that Page 14 (/profile) writes to:
  - display_name VARCHAR(64) — the human-readable name shown in the UI
  - avatar_url TEXT — public URL of the user's avatar (OSS / S3)

Both default to NULL so existing rows are unaffected. No index
needed (low cardinality, accessed only on profile reads).

TASK T1.3d / DESIGN §4.2 users row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_users_profile_fields"
down_revision: str | Sequence[str] | None = "0006_users_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add display_name + avatar_url as nullable columns."""
    op.add_column(
        "users",
        sa.Column("display_name", sa.String(64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("avatar_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Reverse: drop both columns (any stored data is lost)."""
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "display_name")