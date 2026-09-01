"""Tests for the `POST /chat/query` HTTP route (T3 wire-up).

Drives the FastAPI app via `TestClient` with:
  * the chat service factory replaced by a programmable fake
  * `get_current_user` overridden to a canned `UserContext`
  * `get_db` overridden to a fake session
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from agentic_rag_project.api_gateway import chat_router
from agentic_rag_project.api_gateway.dependencies import UserContext
from agentic_rag_project.api_gateway.jwt import create_access_token
from agentic_rag_project.chat import (
    ChatQueryResponse,
    CitationItem,
    StepItem,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_ctx() -> UserContext:
    """A canned `UserContext` so the route accepts our fake requests."""
    return UserContext(
        user_id=uuid.uuid4(),
        username="alice",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({uuid.uuid4()}),
        permissions=frozenset({"chat:query"}),
    )


@pytest.fixture
def fake_service():
    """Stand-in for ChatService exposing the methods the router calls."""

    class _Service:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []
            self.next_response: ChatQueryResponse | None = None
            self.next_exception: BaseException | None = None

        def handle(self, **kwargs) -> ChatQueryResponse:
            self.calls.append(kwargs)
            if self.next_exception is not None:
                raise self.next_exception
            assert self.next_response is not None
            return self.next_response

    return _Service()


class _FakeSession:
    """Minimal SQLAlchemy session stand-in for chat_router FK lookup tests.

    Only implements `get(Model, pk)` + `commit()` (no-op). `_resolve_workspace_id`
    needs `get` to verify the workspace row exists. `known_workspaces`
    maps `pk -> Model`; lookup returns None when missing (matches
    SQLAlchemy semantics). Tests that override `user_ctx` should also
    call `fake_session.add_known(ws_id)` so the FK check accepts the
    override UUID.
    """

    def __init__(self, known_workspaces: dict[uuid.UUID, Any] | None = None):
        self.known_workspaces: dict[uuid.UUID, Any] = known_workspaces or {}

    def add_known(self, ws_id: uuid.UUID) -> None:
        """Register a workspace id as existing (for tests that swap ctx)."""
        self.known_workspaces[ws_id] = object()

    def get(self, model: type, pk: Any) -> Any:
        # `_resolve_workspace_id` only ever asks for Workspace by UUID.
        return self.known_workspaces.get(pk)

    def commit(self) -> None:
        """No-op — the fake session never persists."""
        return None


@pytest.fixture
def fake_session(user_ctx: UserContext) -> _FakeSession:
    """A session that knows about the user's workspace (so happy-path
    override-lookup also finds the row)."""
    member_ws = next(iter(user_ctx.workspace_ids))
    return _FakeSession(known_workspaces={member_ws: object()})


@pytest.fixture
def app(user_ctx: UserContext, fake_service, fake_session):
    """A minimal FastAPI app exposing only the chat router with overrides."""
    app = FastAPI()
    app.include_router(chat_router.get_router(), prefix="/api/v1")

    # Override auth + DB.
    app.dependency_overrides[chat_router.get_current_user] = lambda: user_ctx
    app.dependency_overrides[chat_router.get_db] = lambda: fake_session

    # We can rely on the chat_service_factory indirection instead of
    # overriding the FastAPI dep directly.
    chat_router.set_chat_service_factory(lambda: fake_service)
    yield app
    chat_router.set_chat_service_factory(None)  # reset


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def raw_app():
    """A FastAPI app without auth overrides — used to verify 401."""
    app = FastAPI()
    app.include_router(chat_router.get_router(), prefix="/api/v1")
    # Wire a dummy factory so dependency injection succeeds past auth.
    chat_router.set_chat_service_factory(
        lambda: pytest.fail("auth should fail before service is called")
    )
    yield app
    chat_router.set_chat_service_factory(None)


def _auth_header(user_ctx: UserContext) -> dict[str, str]:
    token = create_access_token(user_ctx.user_id)
    return {"Authorization": f"Bearer {token}"}


def _ok_response(conv_id: str | None = None) -> ChatQueryResponse:
    return ChatQueryResponse(
        conversation_id=conv_id or str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
        answer="hello world",
        citations=[
            CitationItem(
                chunk_id="c-1",
                document_name="report.pdf",
                page_no=2,
                relevance_score=0.9,
            )
        ],
        steps=[StepItem(step_id="s1", iteration=0, node="plan", action="ok")],
        route="agent",
        iterations=2,
        fallback_triggered=False,
        truncated_by_max_iter=False,
        refused=False,
        redactions={},
        metadata={"source": "agent"},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_query_returns_response(
    client: TestClient, user_ctx: UserContext, fake_service
) -> None:
    fake_service.next_response = _ok_response()
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "what?"},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "hello world"
    assert body["route"] == "agent"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["chunk_id"] == "c-1"
    # The service was invoked with the resolved workspace + user.
    assert len(fake_service.calls) == 1
    call = fake_service.calls[0]
    assert call["user_id"] == user_ctx.user_id
    assert call["workspace_id"] == next(iter(user_ctx.workspace_ids))
    assert call["request"].query == "what?"


def test_query_uses_caller_supplied_workspace(
    client: TestClient, fake_service, fake_session
) -> None:
    explicit_workspace = uuid.uuid4()
    user_ctx_local = UserContext(
        user_id=uuid.uuid4(),
        username="alice",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({explicit_workspace}),
        permissions=frozenset({"chat:query"}),
    )
    # Replace the auth override to use the local ctx.
    client.app.dependency_overrides[chat_router.get_current_user] = (
        lambda: user_ctx_local
    )
    # FK fallback (added 2026-09-01): the override UUID must also be
    # registered with the fake session so the route's workspace existence
    # check accepts it.
    fake_session.add_known(explicit_workspace)

    fake_service.next_response = _ok_response()
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(explicit_workspace)},
        headers=_auth_header(user_ctx_local),
    )
    assert resp.status_code == 200
    assert fake_service.calls[0]["workspace_id"] == explicit_workspace


def test_query_passes_conversation_id_through(
    client: TestClient, user_ctx: UserContext, fake_service
) -> None:
    fake_service.next_response = _ok_response(conv_id="conv-123")
    conv = str(uuid.uuid4())
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "conversation_id": conv},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] == "conv-123"
    assert fake_service.calls[0]["request"].conversation_id == conv


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


def test_query_without_token_returns_401(
    raw_app, fake_service
) -> None:
    """Without an auth override, missing/invalid tokens → 401."""
    client = TestClient(raw_app)
    resp = client.post("/api/v1/chat/query", json={"query": "hi"})
    assert resp.status_code == 401


def test_query_with_empty_query_returns_400(
    client: TestClient, user_ctx: UserContext, fake_service
) -> None:
    from agentic_rag_project.chat import EmptyQueryError

    fake_service.next_exception = EmptyQueryError("query must be a non-empty string")
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": ""},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_query_with_invalid_workspace_returns_400(
    client: TestClient, user_ctx: UserContext
) -> None:
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": "not-a-uuid"},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 400


def test_query_with_unauthorized_workspace_returns_403(
    client: TestClient, user_ctx: UserContext
) -> None:
    other_workspace = uuid.uuid4()  # not in user_ctx.workspace_ids
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(other_workspace)},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Service error mapping
# ---------------------------------------------------------------------------


def test_chat_service_error_returns_500(
    client: TestClient, user_ctx: UserContext, fake_service
) -> None:
    from agentic_rag_project.chat import ChatServiceError

    fake_service.next_exception = ChatServiceError("agent runner died")
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi"},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 500
    assert "agent runner died" in resp.json()["detail"]


def test_unexpected_exception_returns_500(
    client: TestClient, user_ctx: UserContext, fake_service
) -> None:
    fake_service.next_exception = RuntimeError("kaboom")
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi"},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal_error"


# ---------------------------------------------------------------------------
# Max-iterations validation (Pydantic-side)
# ---------------------------------------------------------------------------


def test_query_max_iterations_out_of_range(
    client: TestClient, user_ctx: UserContext
) -> None:
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "max_iterations": 0},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 422  # Pydantic validation


def test_query_max_iterations_too_high(
    client: TestClient, user_ctx: UserContext
) -> None:
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "max_iterations": 999},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# FK fallback (added 2026-09-01 for M6 /chat smoke)
#
# When the caller sends a `workspace_id` that is a syntactically valid
# UUID but does NOT correspond to a row in `workspaces`, the route must
# reject with 400 instead of letting `ChatService.handle` blow up with
# an IntegrityError (which surfaced as a confusing 500 in the
# M6 hand-test). Membership check fires FIRST (so 403 wins for an
# authorized user sending a foreign UUID); the FK check catches the
# "valid UUID, but no such row" case — typically the placeholder
# `00000000-...` from the frontend's `workspace.ensureFallback()`.
# ---------------------------------------------------------------------------


def test_query_with_unknown_workspace_id_returns_400(
    client: TestClient, user_ctx: UserContext
) -> None:
    """Super-admin (no membership restriction) sends a UUID that isn't
    in the workspaces table → 400, not 500. Real-world trigger: the
    frontend's placeholder UUID when `/me` doesn't return workspaces."""
    other_workspace = uuid.uuid4()  # valid UUID, but no row exists

    super_admin_ctx = UserContext(
        user_id=user_ctx.user_id,
        username=user_ctx.username,
        is_super_admin=True,
        status="enable",
        workspace_ids=frozenset(),
        permissions=user_ctx.permissions,
    )
    client.app.dependency_overrides[chat_router.get_current_user] = (
        lambda: super_admin_ctx
    )
    # Override auth header too so token matches the new ctx.
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(other_workspace)},
        headers=_auth_header(super_admin_ctx),
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_query_with_placeholder_workspace_id_returns_400(
    client: TestClient, user_ctx: UserContext
) -> None:
    """The exact M6 hand-test bug: super_admin + workspace_id =
    '00000000-0000-0000-0000-000000000000' (frontend fallback) used
    to produce 500 via IntegrityError on INSERT Conversation. Now 400."""
    placeholder = uuid.UUID("00000000-0000-0000-0000-000000000000")

    super_admin_ctx = UserContext(
        user_id=user_ctx.user_id,
        username=user_ctx.username,
        is_super_admin=True,
        status="enable",
        workspace_ids=frozenset(),
        permissions=user_ctx.permissions,
    )
    client.app.dependency_overrides[chat_router.get_current_user] = (
        lambda: super_admin_ctx
    )
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(placeholder)},
        headers=_auth_header(super_admin_ctx),
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_query_403_still_wins_over_400_for_unknown_workspace(
    client: TestClient, user_ctx: UserContext
) -> None:
    """A non-super-admin sending a UUID that isn't a member AND
    doesn't exist → 403 (membership check fires first). FK check
    is a no-op for unauthorized workspaces."""
    other_workspace = uuid.uuid4()
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(other_workspace)},
        headers=_auth_header(user_ctx),
    )
    assert resp.status_code == 403


def test_query_with_known_override_workspace_id_returns_200(
    client: TestClient, user_ctx: UserContext, fake_service, fake_session
) -> None:
    """Sanity: a caller-override UUID that EXISTS in the workspaces
    table still flows through normally — the FK fallback is additive,
    not breaking the happy path."""
    explicit_workspace = uuid.uuid4()
    user_ctx_local = UserContext(
        user_id=user_ctx.user_id,
        username=user_ctx.username,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({explicit_workspace}),
        permissions=user_ctx.permissions,
    )
    client.app.dependency_overrides[chat_router.get_current_user] = (
        lambda: user_ctx_local
    )
    fake_session.add_known(explicit_workspace)

    fake_service.next_response = _ok_response()
    resp = client.post(
        "/api/v1/chat/query",
        json={"query": "hi", "workspace_id": str(explicit_workspace)},
        headers=_auth_header(user_ctx_local),
    )
    assert resp.status_code == 200
