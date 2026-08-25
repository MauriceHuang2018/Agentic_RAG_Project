"""create users.api_tokens (M6 Page 14 / T5.7)

Revision ID: 0008_users_api_tokens
Revises: 0007_users_profile_fields
Create Date: 2026-08-25

Adds the `api_tokens` table that backs user-scoped API tokens:
  - token_hash CHAR(64) stores the SHA-256 hex of the plaintext
    (plaintext is shown to the user ONLY at creation time, never
    persisted)
  - token_prefix VARCHAR(8) is the first 8 chars of the plaintext
    used by the UI to identify which token is which
  - expires_at / revoked_at track lifecycle

The lookup index on (token_prefix, token_hash) supports the
`api_gateway/dependencies.py::get_current_user` `sk-` branch.

TASK T1.3e / DESIGN §4.6.4.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_users_api_tokens"
down_revision: str | Sequence[str] | None = "0007_users_profile_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create api_tokens + 2 indexes (user_id lookup + token lookup)."""
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_user", "api_tokens", ["user_id"])
    op.create_index(
        "ix_api_tokens_lookup",
        "api_tokens",
        ["token_prefix", "token_hash"],
    )


def downgrade() -> None:
    """Reverse: drop indexes first, then the table."""
    op.drop_index("ix_api_tokens_lookup", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user", table_name="api_tokens")
    op.drop_table("api_tokens")