"""extend drift_alerts with T4.4 detector columns

Revision ID: 0002_drift_events_columns
Revises: 0001_initial_schema
Create Date: 2026-08-22

Adds the per-kind / per-metric / per-severity breakdown the T4.4
drift_detector needs, plus two composite indexes for the common
query patterns ("recent critical drift" and "workspace's drift
timeline for a metric"). The original `category` column is kept
for back-compat — newer rows use `kind` + `metric_name`.

All new columns carry `server_default` so the migration is safe to
apply to a non-empty table (production never wrote to drift_alerts
before T4.4, but the migration is the contract).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_drift_events_columns"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add 5 new columns + 2 composite indexes to drift_alerts."""
    op.add_column(
        "drift_alerts",
        sa.Column(
            "kind",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'eval_quality'"),
        ),
    )
    op.add_column(
        "drift_alerts",
        sa.Column(
            "metric_name",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        "drift_alerts",
        sa.Column(
            "scope",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'workspace'"),
        ),
    )
    op.add_column(
        "drift_alerts",
        sa.Column(
            "drift_pct",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
    )
    op.add_column(
        "drift_alerts",
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'info'"),
        ),
    )
    # Indexes
    op.create_index(
        "ix_drift_alerts_severity_ts",
        "drift_alerts",
        ["severity", sa.text("triggered_at DESC")],
    )
    op.create_index(
        "ix_drift_alerts_workspace_metric",
        "drift_alerts",
        ["workspace_id", "metric_name", sa.text("triggered_at DESC")],
    )


def downgrade() -> None:
    """Reverse the T4.4 extensions — drop indexes first, then columns."""
    op.drop_index("ix_drift_alerts_workspace_metric", table_name="drift_alerts")
    op.drop_index("ix_drift_alerts_severity_ts", table_name="drift_alerts")
    op.drop_column("drift_alerts", "severity")
    op.drop_column("drift_alerts", "drift_pct")
    op.drop_column("drift_alerts", "scope")
    op.drop_column("drift_alerts", "metric_name")
    op.drop_column("drift_alerts", "kind")
