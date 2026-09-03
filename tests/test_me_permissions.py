"""Tests for `GET /api/v1/me/permissions` (frontend `rbac.guard` UX gate).

The endpoint closes the pre-existing frontend bug where
`src/web/src/stores/auth.ts::extractPermsFromUser` returned an empty
permissions array for non-super_admin users, sending every chat /
admin route to `/forbidden` even when the user held the right role
bindings server-side. The endpoint mirrors `_resolve_user_context.permissions`
so the frontend can populate `auth.permissions` after login; the
authoritative check still lives in `dependencies.require_permission`.

Covers:
  * alice (chat_user in 2 workspaces) → chat_user perms deduplicated + sorted
  * bob   (kb_admin in 1 workspace)  → kb_admin perms
  * super_admin                       → wildcard `'*'` only
  * user with zero workspace bindings → empty list (NOT 404)
  * anonymous (no auth override)      → 401
  * response shape: `{permissions: list[str]}`
  * end-to-end: POST /auth/login → bearer → GET /me/permissions
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_current_user,
)
from agentic_rag_project.api_gateway.me_router import router as me_router
from agentic_rag_project.api_gateway.router import router as auth_router
from agentic_rag_project.config import get_settings
from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.users import User
from agentic_rag_project.rbac import (
    DEMO_ACME_HQ_ID,
    DEMO_ACME_RD_ID,
    DEMO_ALICE_USERNAME,
    DEMO_BOB_USERNAME,
    seed_builtin_roles,
    seed_demo_data,
)

# Expected permission sets (mirror `rbac/seed.py::RoleSpec.permission_keys`).
CHAT_USER_PERMS = frozenset(
    {"doc:read", "chat:ask", "chat:history:read", "feedback:submit"}
)
KB_ADMIN_PERMS = frozenset(
    {
        "kb:create",
        "kb:update",
        "kb:delete",
        "kb:read",
        "doc:read",
        "doc:write",
        "doc:delete",
        "doc:reindex",
        "audit:read",
    }
)
SUPER_ADMIN_PERMS = frozenset({"*"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory SQLite with `seed_demo_data` materialised.

    Same pattern as `tests/test_me_workspaces.py::session` so the
    demo row layout matches what the lifespan would produce in a
    real boot.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    seed_builtin_roles(s)
    seed_demo_data(s)
    try:
        yield s
    finally:
        s.close()


def _user_id(session: Session, username: str) -> uuid.UUID:
    """Resolve a demo username to its user UUID."""
    return session.execute(
        select(User).where(User.username == username)
    ).scalar_one().id


def _build_app(session: Session, ctx: UserContext | None) -> FastAPI:
    """Build a FastAPI app with the me_router mounted + auth/db overrides.

    Pass `ctx=None` to skip the auth override (so the missing-bearer
    401 path can be exercised); otherwise the supplied `UserContext`
    is what `get_current_user` returns.
    """
    app = FastAPI()
    app.include_router(me_router, prefix="/api/v1")
    if ctx is not None:
        app.dependency_overrides[get_current_user] = lambda: ctx
    app.dependency_overrides[
        __import__(
            "agentic_rag_project.db.session",
            fromlist=["get_db"],
        ).get_db
    ] = lambda: session
    return app


def _client_for(session: Session, ctx: UserContext | None) -> TestClient:
    return TestClient(_build_app(session, ctx))


# ---------------------------------------------------------------------------
# Happy path — alice (chat_user in two workspaces)
# ---------------------------------------------------------------------------


def test_me_permissions_alice_returns_chat_user_keys(session: Session) -> None:
    """alice is bound to acme-hq + acme-rd as chat_user; the deduped
    union of the chat_user role keys comes back sorted."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=CHAT_USER_PERMS,
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/permissions")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["permissions"]) == CHAT_USER_PERMS
    # Sorted alphabetically so the response is deterministic.
    assert body["permissions"] == sorted(CHAT_USER_PERMS)


# ---------------------------------------------------------------------------
# Different role — bob (kb_admin in one workspace)
# ---------------------------------------------------------------------------


def test_me_permissions_bob_returns_kb_admin_keys(session: Session) -> None:
    """bob is bound only to acme-hq as kb_admin; his key set is
    larger and must NOT contain chat-only keys like `feedback:submit`."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_BOB_USERNAME),
        username=DEMO_BOB_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID}),
        permissions=KB_ADMIN_PERMS,
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/permissions")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["permissions"]) == KB_ADMIN_PERMS
    # chat-only key stays out for bob (he's not a chat_user).
    assert "feedback:submit" not in body["permissions"]


# ---------------------------------------------------------------------------
# Super-admin wildcard
# ---------------------------------------------------------------------------


def test_me_permissions_super_admin_returns_wildcard(session: Session) -> None:
    """Super-admin context carries the wildcard `'*'` (matches
    `PERMISSION_WILDCARD` in `src/web/src/constants/permissions.ts`);
    the response surfaces it literally so the frontend rbac.guard
    short-circuits without inspecting `is_super_admin` separately."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=True,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=SUPER_ADMIN_PERMS,
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/permissions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["permissions"] == ["*"]


# ---------------------------------------------------------------------------
# Empty permissions — user with no role bindings
# ---------------------------------------------------------------------------


def test_me_permissions_empty_when_no_roles(session: Session) -> None:
    """A user with no role bindings (frozen empty `permissions`)
    gets `[]` (NOT 404). Frontend treats this as 'no routes gated
    by perms are accessible'."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset(),
        permissions=frozenset(),
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/permissions")

    assert resp.status_code == 200
    assert resp.json() == {"permissions": []}


# ---------------------------------------------------------------------------
# Auth required — no override → 401
# ---------------------------------------------------------------------------


def test_me_permissions_requires_auth(session: Session) -> None:
    """Without a `get_current_user` override, the bearer dependency
    raises 401 — verifies the route is actually wired through the
    auth dep and didn't accidentally become public."""
    client = _client_for(session, ctx=None)

    resp = client.get("/api/v1/me/permissions")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_me_permissions_response_shape(session: Session) -> None:
    """Top-level body has exactly `{permissions: list[str]}`."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=CHAT_USER_PERMS,
    )
    client = _client_for(session, ctx)

    body = client.get("/api/v1/me/permissions").json()
    assert set(body.keys()) == {"permissions"}
    assert isinstance(body["permissions"], list)
    for key in body["permissions"]:
        assert isinstance(key, str)
    # No duplicates — backend frozenset guarantees it.
    assert len(body["permissions"]) == len(set(body["permissions"]))


# ---------------------------------------------------------------------------
# Deduplication — identical keys from two workspaces collapse to one
# ---------------------------------------------------------------------------


def test_me_permissions_dedupes_across_workspaces(session: Session) -> None:
    """alice's chat_user role keys appear in BOTH workspaces; the
    backend returns the union (no duplicates) — the frontend
    `auth.permissions.includes(key)` set-check must not double-count."""
    # Build a permissions set that intentionally contains a duplicate
    # entry (defensive: in production `_resolve_user_context` always
    # produces a set, but the endpoint contract must hold even if a
    # caller passes a multi-set by mistake).
    dupe = frozenset(CHAT_USER_PERMS | {"chat:ask"})
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=dupe,
    )
    client = _client_for(session, ctx)

    body = client.get("/api/v1/me/permissions").json()
    # `frozenset` already collapses `chat:ask`; the sorted output must
    # contain each key exactly once.
    assert body["permissions"].count("chat:ask") == 1
    assert len(body["permissions"]) == len(set(body["permissions"]))


# ---------------------------------------------------------------------------
# End-to-end — POST /auth/login → bearer → GET /me/permissions
# ---------------------------------------------------------------------------


def _build_full_app(session: Session) -> FastAPI:
    """Build the full gateway router stack (auth + me) on a single
    FastAPI app, wiring `get_db` to the seeded session but NOT
    overriding `get_current_user` — so the real JWT decode path is
    exercised end-to-end."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.dependency_overrides[
        __import__(
            "agentic_rag_project.db.session",
            fromlist=["get_db"],
        ).get_db
    ] = lambda: session
    return app


def test_login_then_me_permissions_alice_returns_real_perms(
    session: Session,
) -> None:
    """Reproduces the alice flow the frontend exercises: POST /auth/login
    → bearer → GET /me/permissions. The returned keys must be the
    real chat_user permission keys so `rbac.guard` lets alice reach
    /chat (permKey `chat:ask`) without any super_admin workaround.
    """
    settings = get_settings()
    client = TestClient(_build_full_app(session))

    # 1) /auth/login with alice's seeded credentials.
    login_resp = client.post(
        "/api/v1/auth/login",
        data={
            "username": DEMO_ALICE_USERNAME,
            "password": settings.demo_alice_password,
        },
    )
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["access_token"]

    # 2) /me/permissions with the bearer.
    me_resp = client.get(
        "/api/v1/me/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_resp.status_code == 200, me_resp.text
    body = me_resp.json()

    # 3) The keys must include the route-level gate that the bug
    #    previously blocked: `/chat` requires `chat:ask`.
    assert "chat:ask" in body["permissions"], body
    # And the chat_user set the seed grants her.
    assert set(body["permissions"]) == CHAT_USER_PERMS
