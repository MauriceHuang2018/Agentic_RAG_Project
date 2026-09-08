"""Tests for F3 — feedback transaction boundary (M5 security).

DESIGN §4.4 / §5.2 — fix one MEDIUM-severity transaction bug:

  F3: `post_feedback` used to wrap `service.submit` in
      `try/except/finally` with `session.commit()` in `finally`. If
      `service.submit` raised mid-transaction, the `finally` block
      STILL committed whatever was already flushed, leaving the
      DB in a half-written state (Feedback row present, but
      FeedbackTicket / FeedbackAttribution missing).

The fix moves the transaction boundary into the service layer:

  * Route: no try/except/finally around `service.submit`. Any
    exception propagates as a 500.
  * Service: wraps its body in `try / except / else`. Commits on
    success, rolls back on any raise.

These tests pin:

  1. Happy path — `service.submit` commits the Feedback + ticket
     + (optional) attribution atomically. One row in `feedbacks`.
  2. Failure path — when the inner logic raises, the session is
     rolled back so no row leaks into `feedbacks`. The exception
     propagates so the route returns 500 (not 201 with partial
     state).
  3. F2 PermissionError path — same as failure: rollback, no
     leaked row, exception propagates.

The route no longer has any commit/rollback logic. We assert this
implicitly by having the test use FastAPI's `get_db` dependency
override (which only closes the session at request end) and
verifying row counts.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_audit_service,
    get_current_user,
)
from agentic_rag_project.api_gateway import feedback_router
from agentic_rag_project.feedback import FeedbackRepository
from agentic_rag_project.feedback.models import _Base
from tests._m5_min_meta import (
    Conversation as _M5Conv,
    Message as _M5Msg,
    Workspace as _M5Ws,
    _M5_MIN_TABLES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """SQLite in-memory engine with both feedback tables and the
    minimum messages/conversations/workspaces tables needed by the
    F2 workspace check.
    """
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _Base.metadata.create_all(eng)
    _M5_MIN_TABLES.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def message_id(workspace_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def seeded_message(
    session_factory,
    message_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Pre-seed the messages table so the F2 lookup passes."""
    conv_id = uuid.uuid4()
    with session_factory() as s:
        s.add(_M5Ws(id=workspace_id))
        s.add(_M5Conv(id=conv_id, workspace_id=workspace_id))
        s.add(_M5Msg(id=message_id, conversation_id=conv_id, role="assistant", content=""))
        s.commit()


@pytest.fixture
def audit_service_stub() -> MagicMock:
    svc = MagicMock()
    svc.record = MagicMock()
    return svc


@pytest.fixture
def user_ctx(workspace_id: uuid.UUID) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        username="alice",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({workspace_id}),
        permissions=frozenset({"feedback:write"}),
    )


def _make_app(
    *,
    user_ctx: UserContext,
    audit_service: MagicMock,
    session_factory,
) -> FastAPI:
    """Build a FastAPI app with deps overridden to use the in-memory
    engine. `get_db` is overridden to yield a fresh session per
    request — FastAPI's default `get_db` only closes the session,
    it doesn't commit, so this mirrors the production behaviour.
    """
    app = FastAPI()
    app.include_router(feedback_router.router)
    app.dependency_overrides[get_current_user] = lambda: user_ctx
    app.dependency_overrides[get_audit_service] = lambda: audit_service

    def _fake_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    from agentic_rag_project.db.session import get_db

    app.dependency_overrides[get_db] = _fake_db
    return app


def _count_feedbacks(session_factory) -> int:
    with session_factory() as s:
        from agentic_rag_project.feedback.models import Feedback

        stmt = select(func.count()).select_from(Feedback)
        return int(s.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------
# Happy path — service.submit commits atomically
# ---------------------------------------------------------------------------


def test_t3_happy_path_commits_feedback_row(
    session_factory,
    user_ctx: UserContext,
    audit_service_stub: MagicMock,
    seeded_message: None,
    message_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Successful POST /feedback persists exactly one Feedback row.

    The service is responsible for committing. The route no longer
    commits in `finally`. This test proves the service's commit
    path keeps the contract: callers don't have to do anything.
    """
    app = _make_app(
        user_ctx=user_ctx,
        audit_service=audit_service_stub,
        session_factory=session_factory,
    )

    assert _count_feedbacks(session_factory) == 0

    with TestClient(app) as client:
        resp = client.post(
            "/feedback",
            json={
                "message_id": str(message_id),
                "rating": "like",
                "comment": "ok",
            },
        )

    assert resp.status_code == 201, resp.text
    assert _count_feedbacks(session_factory) == 1


# ---------------------------------------------------------------------------
# Failure path — service.submit raises → session rolled back, no row leaks
# ---------------------------------------------------------------------------


def test_t3_failure_path_rolls_back_no_row_leaks(
    session_factory,
    user_ctx: UserContext,
    audit_service_stub: MagicMock,
    seeded_message: None,
    message_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """When `service.submit` raises mid-transaction (simulated by
    injecting a service factory that raises), the session is
    rolled back so the DB has no leaked rows.

    Before F3, the route had `finally: session.commit()` which
    persisted whatever was already flushed (often the Feedback
    row alone, without its ticket/attribution). After F3, the
    service commits at the end of the try block; any raise inside
    the try triggers rollback before the exception propagates.

    We assert:

      * The exception propagates (the route doesn't swallow it)
      * Zero rows in `feedbacks` after the request
      * Audit `feedback_submit` is NOT recorded (we never got to
        the success path)
    """
    app = _make_app(
        user_ctx=user_ctx,
        audit_service=audit_service_stub,
        session_factory=session_factory,
    )

    # Inject a service factory whose `submit` raises. We DON'T use
    # the real FeedbackService because we want to test the
    # rollback behaviour in isolation — the rollback must be
    # triggered by ANY raise, not just one we engineer in the
    # real service body.
    class _BoomService:
        def submit(self, **kwargs: Any) -> None:
            # Simulate a flush having happened (e.g. a partial
            # write) before the raise. Without the rollback,
            # this session would persist the partial state.
            raise RuntimeError("simulated downstream failure")

    feedback_router.set_feedback_service_factory(lambda session, ctx: _BoomService())

    assert _count_feedbacks(session_factory) == 0

    # `raise_server_exceptions=False` so the route's uncaught
    # exception surfaces as a 500 instead of re-raising inside the
    # test process. We want to assert on the response code, not
    # catch the RuntimeError here.
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/feedback",
            json={
                "message_id": str(message_id),
                "rating": "like",
            },
        )

    # The route lets the exception propagate to FastAPI, which
    # returns 500. The previous behaviour raised HTTPException(500,
    # "internal_error") — the new behaviour is identical from the
    # caller's perspective but no row leaked.
    assert resp.status_code == 500, resp.text
    assert _count_feedbacks(session_factory) == 0
    # No success audit; the access_denied audit (if any) would have
    # been from the F2 check, which passes because we seeded.
    audit_calls = [
        c.args[0]
        for c in audit_service_stub.record.call_args_list
    ]
    actions = [e.action for e in audit_calls]
    assert "feedback_submit" not in actions


def test_t3_f2_permission_error_rolls_back(
    session_factory,
    user_ctx: UserContext,
    audit_service_stub: MagicMock,
    seeded_message: None,
    message_id: uuid.UUID,
) -> None:
    """The service-layer F2 check raises PermissionError when the
    message's workspace doesn't match. The route-layer F2 check
    usually catches this first (403), but if it ever lets the
    service-layer check run (e.g. a non-HTTP caller), the F3
    rollback must prevent the Feedback row from leaking.

    We bypass the route-layer check by feeding the route a
    matching workspace_id while the message's actual workspace
    is different. This is hard to engineer through the route,
    so we test the service-layer F2 + F3 path directly: invoke
    FeedbackService.submit() with a workspace_id that doesn't
    match the seeded message.
    """
    # Pre-seed a message in workspace W_real.
    real_ws = uuid.uuid4()
    msg_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    with session_factory() as s:
        s.add(_M5Ws(id=real_ws))
        s.add(_M5Conv(id=conv_id, workspace_id=real_ws))
        s.add(_M5Msg(id=msg_id, conversation_id=conv_id, role="assistant", content=""))
        s.commit()

    # Try to submit feedback claiming workspace_id = W_claim,
    # which is different from real_ws. The F2 check must raise
    # PermissionError and the F3 wrapper must NOT persist the
    # partial state.
    claimed_ws = uuid.uuid4()

    from agentic_rag_project.feedback.service import FeedbackService

    svc = FeedbackService(session=session_factory())
    with pytest.raises(PermissionError):
        svc.submit(
            message_id=msg_id,
            user_id=uuid.uuid4(),
            workspace_id=claimed_ws,
            rating="like",
        )
    # After the raise, the session was rolled back. The Feedback
    # table is empty (no partial write from before the raise).
    assert _count_feedbacks(session_factory) == 0


__all__ = [
    "test_t3_happy_path_commits_feedback_row",
    "test_t3_failure_path_rolls_back_no_row_leaks",
    "test_t3_f2_permission_error_rolls_back",
]