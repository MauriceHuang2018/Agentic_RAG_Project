"""create users.password_reset_audit (M6 Page 14 / T5.7)

Revision ID: 0009_users_password_reset_audit
Revises: 0008_users_api_tokens
Create Date: 2026-08-25

Adds the `password_reset_audit` table that records every password
reset, whether triggered by an admin (admin_force) or by the user
themselves (user_self) or by the system during seed (system_seed).

The composite index `(user_id, reset_at DESC)` powers Page 14's
"操作日志 Tab" query: "show me my last 90 days of resets".

TASK T1.3f / DESIGN §4.2 password_reset_audit row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_users_password_reset_audit"
down_revision: str | Sequence[str] | None = "0008_users_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create password_reset_audit + composite index on (user_id, reset_at DESC)."""
    op.create_table(
        "password_reset_audit",
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
        sa.Column(
            "reset_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reset_method",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'admin_force'"),
        ),
        sa.Column("source_ip", sa.String(45), nullable=True),
        sa.Column(
            "reset_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "reset_method IN ('admin_force', 'user_self', 'system_seed')",
            name="ck_password_reset_audit_method",
        ),
    )
    op.create_index(
        "ix_password_reset_audit_user",
        "password_reset_audit",
        ["user_id", sa.text("reset_at DESC")],
    )


def downgrade() -> None:
    """Reverse: drop index, then table."""
    op.drop_index(
        "ix_password_reset_audit_user", table_name="password_reset_audit"
    )
    op.drop_table("password_reset_audit")