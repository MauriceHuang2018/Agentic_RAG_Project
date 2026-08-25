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


@pytest.fixture
def app(user_ctx: UserContext, fake_service):
    """A minimal FastAPI app exposing only the chat router with overrides."""
    app = FastAPI()
    app.include_router(chat_router.get_router(), prefix="/api/v1")

    # Override auth + DB.
    app.dependency_overrides[chat_router.get_current_user] = lambda: user_ctx

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
    client: TestClient, fake_service
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
