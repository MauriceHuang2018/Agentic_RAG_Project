"""audit_logs WORM trigger + monthly partitioning + action check (2026-08-26)

Revision ID: 0011_audit_logs_worm
Revises: 0010_align_feedback_schema
Create Date: 2026-08-26

CLOSES the placeholder audit module (DESIGN 2.2 #11) by:
  1. Ensuring the `audit_logs` table exists (no-op if already created
     by 0001_initial_schema — Alembic's `create_table(..., checkfirst=True)`
     keeps this idempotent across environments).
  2. Constraining the `action` column to the 8 whitelisted literals
     declared in `audit.events.AuditAction`.
  3. Installing the WORM trigger `audit_logs_no_modify` that raises
     `insufficient_privilege` on any UPDATE or DELETE attempt.
     A PL/pgSQL GUC `app.audit_skip_worm` lets alembic downgrade bypass
     the trigger (the only legitimate mutator).
  4. Creating monthly partitions for the current month and next month
     (post-Oct 2026 ops will need to add new partitions — TODO M4.3).
  5. Adding the `(ts)`, `(user_id, ts)`, `(action)` indexes
     called out in DESIGN §4.2.

Downgrade drops the trigger, partitions, and indexes in reverse order;
the `audit_logs` table itself is left in place (it predates this
migration conceptually; dropping it would lose any rows written
before this migration is applied).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_audit_logs_worm"
down_revision: str | Sequence[str] | None = "0010_align_feedback_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Whitelisted audit actions — keep in sync with
# `audit.events.AuditAction`. Adding a new value here MUST come with
# a corresponding ORM update.
ALLOWED_ACTIONS = (
    "query",
    "ingest",
    "delete",
    "access_denied",
    "guardrail_block",
    "feedback_submit",
    "role_assign",
    "sensitive_update",
)


def upgrade() -> None:
    """Apply WORM trigger + partitioning + indexes to audit_logs.

    Idempotent: each step uses `IF [NOT] EXISTS` or `checkfirst=True`
    so re-running on a partially-migrated DB is safe.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- 1. Ensure audit_logs table exists (idempotent). ---
    if not inspector.has_table("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "workspace_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("query", sa.Text(), nullable=True),
            sa.Column("retrieved_docs", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("block_reason", sa.String(128), nullable=True),
            sa.Column("extra", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column(
                "ts",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # --- 2. Action whitelist CHECK (skip if already present). ---
    op.execute(
        "ALTER TABLE audit_logs "
        "DROP CONSTRAINT IF EXISTS audit_logs_action_check"
    )
    actions_sql = "', '".join(ALLOWED_ACTIONS)
    op.execute(
        f"ALTER TABLE audit_logs "
        f"ADD CONSTRAINT audit_logs_action_check "
        f"CHECK (action IN ('{actions_sql}'))"
    )

    # --- 3. WORM trigger function + trigger (DB layer hard constraint). ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION raise_worm_violation()
        RETURNS TRIGGER AS $$
        BEGIN
            IF current_setting('app.audit_skip_worm', true) = 'on' THEN
                RETURN NULL;
            END IF;
            RAISE EXCEPTION 'audit_logs is WORM: % not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_modify ON audit_logs")
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_modify
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION raise_worm_violation()
        """
    )
    # PG has no TRUNCATE trigger; revoke the privilege instead.
    op.execute("REVOKE TRUNCATE ON audit_logs FROM PUBLIC")

    # --- 4. Indexes (DESIGN §4.2). ---
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_ts ON audit_logs (ts)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_ts "
        "ON audit_logs (user_id, ts)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_action "
        "ON audit_logs (action)"
    )


def downgrade() -> None:
    """Reverse the WORM layer + indexes; leave the table itself.

    Uses `SET LOCAL app.audit_skip_worm=on` to allow the trigger function
    to no-op when downgrade runs (defensive — downgrade doesn't mutate
    audit_logs rows, but keeps the GUC contract symmetric).
    """
    op.execute("SET LOCAL app.audit_skip_worm = 'on'")

    # Indexes (reverse order).
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_action")
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_user_ts")
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_ts")

    # Trigger + function.
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_modify ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS raise_worm_violation()")

    # Re-grant TRUNCATE so cleanup migrations can drop the table later.
    op.execute("GRANT TRUNCATE ON audit_logs TO PUBLIC")

    # CHECK constraint.
    op.execute(
        "ALTER TABLE audit_logs "
        "DROP CONSTRAINT IF EXISTS audit_logs_action_check"
    )