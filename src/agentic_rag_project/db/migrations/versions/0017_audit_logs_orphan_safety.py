"""audit_logs BEFORE INSERT orphan-safety trigger (2026-09-08)

Revision ID: 0017_audit_logs_orphan_safety
Revises: 0016_create_feedback_dependent_tables
Create Date: 2026-09-08

CLOSES the dead-loop bug surfaced by the celery-worker FK violation
on `audit_logs_user_id_fkey` (postgres + celery-worker logs 2026-09-08):

    173 of 175 unique user_ids in the Redis audit buffer no longer
    exist in the `users` table — they were valid when the events
    were recorded, but the underlying users have since been
    hard-deleted (likely via a `docker compose down -v` + reseed
    cycle that did not clear Redis).

    The application-level pre-flight check in
    `AuditService.flush_buffer` filters these orphans BEFORE the
    bulk INSERT (setting `user_id` / `workspace_id` to NULL). This
    migration adds a SECOND-LINE DB trigger that does the same for
    any code path that bypasses the service layer (e.g. ad-hoc
    SQL inserts, future writers we haven't migrated yet).

What this migration does:

    1. Creates a PL/pgSQL function `audit_logs_orphan_safety()` that
       takes a NEW `audit_logs` row and NULLs out:
         - `user_id`      if it doesn't exist in `users`
         - `workspace_id` if it doesn't exist in `workspaces`
       Columns are nullable (per the 0001 + 0011 schema), so the
       INSERT proceeds after the NULL-out.

    2. Installs a `BEFORE INSERT` row-level trigger
       `audit_logs_orphan_safety` that calls the function above.

    3. SQLite (test path): the migration is a no-op. SQLite doesn't
       support `CREATE TRIGGER` inside the same connection-level
       DDL that alembic uses for SQLite (the project's test path
       always uses fresh in-memory SQLite, so the trigger isn't
       needed — the application pre-flight check is the test-side
       coverage).

Downgrade drops the trigger and the function. The `audit_logs`
table itself is not touched.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_audit_logs_orphan_safety"
down_revision: str | Sequence[str] | None = "0016_create_feedback_dependent_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Function body — installed via op.execute() so the SQL string is
# readable and reviewable in one place. Idempotent via
# `CREATE OR REPLACE FUNCTION`.
_ORPHAN_SAFETY_FN_SQL = """
CREATE OR REPLACE FUNCTION audit_logs_orphan_safety() RETURNS trigger AS $$
BEGIN
    -- NULL out user_id if it references a non-existent users row.
    -- The FK audit_logs_user_id_fkey enforces this on every INSERT;
    -- by NULL-ing here, we keep the historical audit row but lose
    -- the (now-meaningless) actor pointer. The action + extra JSON
    -- still record the original user_id if the application kept it.
    IF NEW.user_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM users WHERE id = NEW.user_id
    ) THEN
        NEW.user_id := NULL;
    END IF;

    -- Same for workspace_id (FK audit_logs_workspace_id_fkey).
    IF NEW.workspace_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM workspaces WHERE id = NEW.workspace_id
    ) THEN
        NEW.workspace_id := NULL;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Trigger DDL — `DROP TRIGGER IF EXISTS` keeps re-runs idempotent.
_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS audit_logs_orphan_safety ON audit_logs;
CREATE TRIGGER audit_logs_orphan_safety
    BEFORE INSERT ON audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION audit_logs_orphan_safety();
"""

# SQLite path: a no-op (see module docstring).
_SQLITE_NOOP_SQL = "SELECT 1;"


def upgrade() -> None:
    """Install the orphan-safety function + trigger on PostgreSQL.

    On SQLite the migration is a no-op — the test path always uses
    fresh in-memory SQLite, and the application-level pre-flight
    check in `AuditService.flush_buffer` is the test-side
    coverage for orphan handling.
    """
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(_ORPHAN_SAFETY_FN_SQL)
        op.execute(_TRIGGER_SQL)
    else:
        op.execute(_SQLITE_NOOP_SQL)


def downgrade() -> None:
    """Drop the trigger + function. Reversible on PostgreSQL only."""
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_orphan_safety ON audit_logs;")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_orphan_safety();")
    else:
        op.execute(_SQLITE_NOOP_SQL)
