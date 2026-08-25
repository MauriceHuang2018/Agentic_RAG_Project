"""add roles.workspace_scoped flag (M6 decision #5/#6)

Revision ID: 0004_roles_workspace_scoped
Revises: 0003
Create Date: 2026-08-25

Adds a boolean column `workspace_scoped` to `roles` so RBAC can
distinguish workspace-scoped roles (the historical default — UserRole
binds user+role+workspace) from system-scoped roles (UserRole binds
user+role+workspace_id=NULL, e.g. the new `system_admin`).

Backfill: every existing row gets `workspace_scoped = TRUE` so the
default semantics are unchanged.

TASK T1.3a / DESIGN §4.6.3.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_roles_workspace_scoped"
down_revision: str | Sequence[str] | None = "0002_drift_events_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `workspace_scoped` to roles with NOT NULL + server_default TRUE."""
    op.add_column(
        "roles",
        sa.Column(
            "workspace_scoped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    # Existing 5 built-in rows backfilled by server_default; no data migration needed.


def downgrade() -> None:
    """Reverse: drop the column. No data preservation needed."""
    op.drop_column("roles", "workspace_scoped")