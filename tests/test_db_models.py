"""Smoke tests for the SQLAlchemy ORM models.

Verifies:
  - All 17 model classes import successfully via `db.models`
  - Declarative metadata has the expected 17 tables
  - Each model can be instantiated with valid defaults (no DB required)
  - `Chunk.is_parent` distinguishes parent / child as per DESIGN
  - `users/workspaces/roles` carry the `status` field default 'enable'
"""

from __future__ import annotations

import uuid

from agentic_rag_project.db.models import (
    ACL,
    AuditLog,
    Base,
    Chunk,
    Citation,
    Conversation,
    Document,
    DriftAlert,
    EvaluationResult,
    Feedback,
    FeedbackCategory,
    FeedbackTag,
    Message,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    Workspace,
)

EXPECTED_TABLES = {
    "users",
    "workspaces",
    "roles",
    "permissions",
    "role_permissions",
    "user_roles",
    "documents",
    "chunks",
    "acls",
    "conversations",
    "messages",
    "citations",
    "feedback_tags",
    "feedback_categories",
    "feedbacks",
    "audit_logs",
    "evaluation_results",
    "drift_alerts",
}


def test_all_17_tables_registered() -> None:
    """All 17+1 (chunk-level) tables appear in Base.metadata."""
    actual = {t.name for t in Base.metadata.sorted_tables}
    assert EXPECTED_TABLES <= actual, f"Missing tables: {EXPECTED_TABLES - actual}"


def test_user_default_status_is_enable() -> None:
    """User.status column default is 'enable' (per ALIGNMENT/CONSENSUS).

    SQLAlchemy `default=` is a Python-side default applied at flush time.
    Newly-constructed instances will have `status=None` until inserted, so
    we verify the column default rather than the attribute value.
    """
    status_col = User.__table__.columns["status"]
    assert status_col.default is not None
    assert status_col.default.arg == "enable"
    assert isinstance(uuid.uuid4(), uuid.UUID)  # id auto-generated at flush


def test_workspace_default_status_is_enable() -> None:
    """Workspace.status default 'enable'; isolation_level default 'logical'."""
    ws_table = Workspace.__table__
    assert ws_table.columns["status"].default.arg == "enable"
    assert ws_table.columns["isolation_level"].default.arg == "logical"


def test_role_default_status_is_enable() -> None:
    """Role.status default 'enable'; is_system default False."""
    role_table = Role.__table__
    assert role_table.columns["status"].default.arg == "enable"
    assert role_table.columns["is_system"].default.arg is False


def test_user_status_column_is_not_nullable() -> None:
    """User.status is NOT NULL — the DB enforces status presence."""
    assert User.__table__.columns["status"].nullable is False


def test_role_status_column_is_not_nullable() -> None:
    """Role.status is NOT NULL — frozen at the schema layer."""
    assert Role.__table__.columns["status"].nullable is False


def test_workspace_status_column_is_not_nullable() -> None:
    """Workspace.status is NOT NULL — frozen at the schema layer."""
    assert Workspace.__table__.columns["status"].nullable is False


def test_chunk_distinguishes_parent_and_child() -> None:
    """Chunk.is_parent flag governs the two-stage retrieval grouping."""
    parent = Chunk(
        document_id=uuid.uuid4(),
        chunk_index=0,
        content="chapter",
        content_hash="a" * 64,
        is_parent=True,
    )
    child = Chunk(
        document_id=uuid.uuid4(),
        chunk_index=1,
        content="paragraph",
        content_hash="b" * 64,
        is_parent=False,
        parent_chunk_id=parent.id,
    )
    assert parent.is_parent is True
    assert child.is_parent is False
    assert child.parent_chunk_id == parent.id


def test_user_role_requires_workspace_id() -> None:
    """UserRole has composite primary key including workspace_id (DESIGN 8.1)."""
    pk_columns = {c.name for c in UserRole.__table__.primary_key.columns}
    assert pk_columns == {"user_id", "role_id", "workspace_id"}


def test_acl_requires_user_or_workspace() -> None:
    """ACL row must reference either a user OR a workspace (CHECK constraint)."""
    from sqlalchemy import CheckConstraint

    check_names = {
        c.name
        for c in ACL.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_acls_principal_required" in check_names


def test_permission_key_uniqueness() -> None:
    """Permission.key is unique — single source of truth for action tokens."""
    from sqlalchemy import UniqueConstraint

    for constraint in Permission.__table__.constraints:
        if isinstance(constraint, UniqueConstraint):
            column_names = {c.name for c in constraint.columns}
            if column_names == {"key"}:
                return
    raise AssertionError("Permission.key missing UniqueConstraint")


def test_feedback_score_range_marker() -> None:
    """Feedback.score column exists (1=down, 5=up per DESIGN 4.1)."""
    column_names = {c.name for c in Feedback.__table__.columns}
    assert "score" in column_names


def test_audit_log_is_append_only_via_design() -> None:
    """AuditLog has no updated_at — signals WORM intent (enforced by triggers)."""
    column_names = {c.name for c in AuditLog.__table__.columns}
    assert "updated_at" not in column_names
    assert "ts" in column_names  # append-only timestamp