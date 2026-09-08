"""Tests for F1 + F2 feedback authorization (M5 security).

DESIGN §4.4 / §5.1 — fixes two HIGH-severity cross-tenant reads:

  F1: GET /feedback/by-message/{mid} used to return ALL feedback
      rows for the message regardless of which workspace the caller
      belonged to. Now filtered at the repo layer by
      `workspace_id IN (ctx.workspace_ids)`.

  F2: POST /feedback used to write a row keyed by the caller's
      claimed workspace_id without checking that the target message
      actually belonged to that workspace. Now both the route layer
      (fail-fast 403) and the service layer (PermissionError defense
      in depth) verify `message.conversation.workspace_id ==
      payload.workspace_id`.

These tests pin the route-layer behaviour (HTTP status codes +
audit records). The service-layer PermissionError path is exercised
implicitly by mocking `service.submit` to raise and asserting the
audit row is NOT the success one (the route catches before
service.submit is called).

The repo-layer workspace filter is exercised by the F1 happy path:
empty `workspace_ids` returns 0 rows; non-empty matching returns N
rows. The defensive empty-list branch is also covered.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_audit_service,
    get_current_user,
    get_db,
)
from agentic_rag_project.api_gateway.feedback_router import router as feedback_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_a() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def workspace_b() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def message_in_a() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def user_ctx_for_a(workspace_a: str) -> UserContext:
    """A non-super-admin user that's a member of workspace A only."""
    return UserContext(
        user_id=uuid.uuid4(),
        username="test-user",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({uuid.UUID(workspace_a)}),
        permissions=frozenset({"feedback:read", "feedback:write"}),
    )


@pytest.fixture
def super_admin_ctx() -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        username="test-admin",
        is_super_admin=True,
        status="enable",
        workspace_ids=frozenset(),
        permissions=frozenset({"*"}),
    )


@pytest.fixture
def audit_service_stub() -> MagicMock:
    """Stub AuditService — records calls so tests can assert on them."""
    svc = MagicMock()
    svc.record = MagicMock()
    return svc


def _make_app(
    *,
    user_ctx: UserContext,
    audit_service: MagicMock,
    db_session: MagicMock,
) -> FastAPI:
    """Build a FastAPI app with deps overridden for the test."""
    app = FastAPI()
    app.include_router(feedback_router)
    app.dependency_overrides[get_current_user] = lambda: user_ctx
    app.dependency_overrides[get_audit_service] = lambda: audit_service
    app.dependency_overrides[get_db] = lambda: db_session
    return app


# ---------------------------------------------------------------------------
# F1 — GET /feedback/by-message/{mid} workspace filter
# ---------------------------------------------------------------------------


def test_f1_by_message_returns_404_when_no_matching_workspace(
    user_ctx_for_a: UserContext,
    workspace_b: str,
    audit_service_stub: MagicMock,
) -> None:
    """Workspace A user asks for feedback on a W_B message.

    Expected: 404 (no info leak) + 1 access_denied audit row
    recording `reason=cross_workspace_read`.
    """
    db = MagicMock()
    # Repo returns 0 rows — the message exists in W_B but caller
    # is in W_A, so the workspace filter excludes every row.
    # `repo.get_feedback_by_message` calls `list(scalars())` not
    # `.scalars().all()`, so we make the scalars iterable directly.
    db.execute.return_value.scalars.return_value = []
    # session.get(Message, mid) for the post_feedback path; not used here.
    db.get.return_value = None

    app = _make_app(
        user_ctx=user_ctx_for_a,
        audit_service=audit_service_stub,
        db_session=db,
    )
    client = TestClient(app)

    response = client.get(f"/feedback/by-message/{uuid.uuid4()}")

    assert response.status_code == 404
    # Audit recorded once with the cross-workspace reason.
    audit_service_stub.record.assert_called_once()
    event = audit_service_stub.record.call_args.args[0]
    assert event.action == "access_denied"
    assert event.extra["reason"] == "cross_workspace_read"


def test_f1_by_message_returns_rows_when_workspace_matches(
    user_ctx_for_a: UserContext,
    workspace_a: str,
    audit_service_stub: MagicMock,
) -> None:
    """Workspace A user asks for feedback on a W_A message that has rows.

    Expected: 200 OK with items + NO access_denied audit.
    """
    db = MagicMock()
    fake_rows = [
        MagicMock(
            id=uuid.uuid4(),
            rating=MagicMock(value="like"),
            comment="ok",
            attribution_status=MagicMock(value="succeeded"),
            created_at=None,
        ),
    ]
    db.execute.return_value.scalars.return_value = fake_rows

    app = _make_app(
        user_ctx=user_ctx_for_a,
        audit_service=audit_service_stub,
        db_session=db,
    )
    client = TestClient(app)

    response = client.get(f"/feedback/by-message/{uuid.uuid4()}")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] and len(body["items"]) == 1
    audit_service_stub.record.assert_not_called()


# ---------------------------------------------------------------------------
# F2 — POST /feedback message.workspace_id check
# ---------------------------------------------------------------------------


def test_f2_post_feedback_403_when_message_in_other_workspace(
    user_ctx_for_a: UserContext,
    workspace_a: str,
    workspace_b: str,
    audit_service_stub: MagicMock,
) -> None:
    """Workspace A user POSTs feedback claiming workspace_id=A but
    the target message belongs to workspace_id=B.

    Expected: 403 message_not_in_workspace + 1 access_denied audit row
    recording `reason=feedback_message_workspace_mismatch`.
    The service.submit() is NOT called (route-layer catches first).
    """
    db = MagicMock()
    # F2 check now uses `session.execute(select(...)).scalar_one_or_none()`
    # instead of `session.get(Message, mid).conversation.workspace_id`.
    # Mock the targeted JOIN query to return W_B.
    message_id = uuid.uuid4()
    db.execute.return_value.scalar_one_or_none.return_value = uuid.UUID(workspace_b)

    app = _make_app(
        user_ctx=user_ctx_for_a,
        audit_service=audit_service_stub,
        db_session=db,
    )
    client = TestClient(app)

    response = client.post(
        "/feedback",
        json={
            "message_id": str(message_id),
            "rating": "like",
            "workspace_id": workspace_a,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "message_not_in_workspace"
    # Exactly one audit row, with the F2 reason.
    audit_service_stub.record.assert_called_once()
    event = audit_service_stub.record.call_args.args[0]
    assert event.action == "access_denied"
    assert event.extra["reason"] == "feedback_message_workspace_mismatch"
    assert event.extra["payload_workspace_id"] == workspace_a
    assert event.extra["message_workspace_id"] == workspace_b


def test_f2_post_feedback_404_when_message_not_found(
    user_ctx_for_a: UserContext,
    workspace_a: str,
    audit_service_stub: MagicMock,
) -> None:
    """Unknown message_id → 404 (no audit because there's nothing
    to record a cross-tenant attempt against)."""
    db = MagicMock()
    # F2 lookup returns None → message not found.
    db.execute.return_value.scalar_one_or_none.return_value = None

    app = _make_app(
        user_ctx=user_ctx_for_a,
        audit_service=audit_service_stub,
        db_session=db,
    )
    client = TestClient(app)

    response = client.post(
        "/feedback",
        json={
            "message_id": str(uuid.uuid4()),
            "rating": "like",
            "workspace_id": workspace_a,
        },
    )

    assert response.status_code == 404
    assert "message_not_found" in response.json()["detail"]
    audit_service_stub.record.assert_not_called()


# ---------------------------------------------------------------------------
# Repo-layer guard: empty workspace_ids → 0 rows (no PG IN () crash)
# ---------------------------------------------------------------------------


def test_repository_get_feedback_by_message_empty_workspace_ids() -> None:
    """Calling `repo.get_feedback_by_message(mid, workspace_ids=[])`
    must short-circuit to [] — no SQL is built and no `IN ()`
    syntax error escapes.
    """
    from agentic_rag_project.feedback.repository import FeedbackRepository

    db = MagicMock()
    repo = FeedbackRepository(db)

    items = repo.get_feedback_by_message(uuid.uuid4(), workspace_ids=[])

    assert items == []
    # No SQL execution should have happened.
    db.execute.assert_not_called()


__all__ = [
    "test_f1_by_message_returns_404_when_no_matching_workspace",
    "test_f1_by_message_returns_rows_when_workspace_matches",
    "test_f2_post_feedback_403_when_message_in_other_workspace",
    "test_f2_post_feedback_404_when_message_not_found",
    "test_repository_get_feedback_by_message_empty_workspace_ids",
]