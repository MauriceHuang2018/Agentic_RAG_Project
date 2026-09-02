"""Tests for `GET /api/v1/me/workspaces` (M6 — frontend workspace picker).

Closed the T6.1 ↔ backend gap (M6): the workspace switcher in
Chat.vue needed real workspace UUIDs; before this endpoint existed
the frontend store hard-coded a `00000000-...` placeholder, every
chat request got rejected with 403, and the user saw an empty
dropdown labelled "primary".

The endpoint delegates membership to `UserContext.workspace_ids`
(already `__system__`-free via `_resolve_user_context`) and joins
the `workspaces` table only for display fields.

Covers:
  * alice → 2 workspaces (acme-hq + acme-rd, both with their demo names)
  * bob → 1 workspace (acme-hq only)
  * super_admin → both workspaces (no `__system__`)
  * user with zero workspaces → []
  * anonymous (no auth override) → 401
  * response shape: id / name / isolation_level / status
  * stable ordering: rows sorted by name case-insensitive
  * end-to-end: POST /auth/login → bearer → GET /me/workspaces
    (real JWT decode, no `get_current_user` override)
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi import Depends, FastAPI
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
    DEMO_ACME_HQ_NAME,
    DEMO_ACME_RD_ID,
    DEMO_ACME_RD_NAME,
    DEMO_ALICE_USERNAME,
    DEMO_BOB_USERNAME,
    seed_builtin_roles,
    seed_demo_data,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory SQLite with `seed_demo_data` materialised.

    Same pattern as `tests/test_demo_data_seed.py::session` so the
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
# Happy path — alice (two workspaces)
# ---------------------------------------------------------------------------


def test_me_workspaces_returns_alice_two_workspaces(session: Session) -> None:
    """alice is bound to acme-hq + acme-rd; both come back."""
    alice_id = _user_id(session, DEMO_ALICE_USERNAME)
    ctx = UserContext(
        user_id=alice_id,
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=frozenset({"chat:ask"}),
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/workspaces")

    assert resp.status_code == 200
    rows = resp.json()
    assert {r["id"] for r in rows} == {str(DEMO_ACME_HQ_ID), str(DEMO_ACME_RD_ID)}
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(DEMO_ACME_HQ_ID)]["name"] == DEMO_ACME_HQ_NAME
    assert by_id[str(DEMO_ACME_RD_ID)]["name"] == DEMO_ACME_RD_NAME


# ---------------------------------------------------------------------------
# Membership visibility — bob (one workspace)
# ---------------------------------------------------------------------------


def test_me_workspaces_returns_bob_one_workspace(session: Session) -> None:
    """bob is bound only to acme-hq; acme-rd must NOT appear."""
    bob_id = _user_id(session, DEMO_BOB_USERNAME)
    ctx = UserContext(
        user_id=bob_id,
        username=DEMO_BOB_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID}),
        permissions=frozenset({"kb:read"}),
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/workspaces")

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(DEMO_ACME_HQ_ID)


# ---------------------------------------------------------------------------
# Super-admin sees every non-system workspace
# ---------------------------------------------------------------------------


def test_me_workspaces_super_admin_sees_all_non_system(session: Session) -> None:
    """Super-admin bypasses per-workspace membership and gets both
    demo workspaces; the designated `__system__` workspace stays
    out (the UserContext builder is responsible for that filter)."""
    # Build the same workspace set `_resolve_user_context` would
    # produce for a super-admin on this seeded DB.
    from agentic_rag_project.rbac.constants import SYSTEM_WORKSPACE_ID

    all_ids = {DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID} - {SYSTEM_WORKSPACE_ID}
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=True,
        status="enable",
        workspace_ids=frozenset(all_ids),
        permissions=frozenset({"*"}),
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/workspaces")

    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ids == {str(DEMO_ACME_HQ_ID), str(DEMO_ACME_RD_ID)}
    # Sanity: no `__system__` row sneaks in via the join either.
    from agentic_rag_project.db.models.users import Workspace as _W
    sys_row = session.execute(
        select(_W).where(_W.id == SYSTEM_WORKSPACE_ID)
    ).scalar_one_or_none()
    if sys_row is not None:
        # If __system__ is in the DB it must NOT appear in the response.
        assert str(SYSTEM_WORKSPACE_ID) not in ids


# ---------------------------------------------------------------------------
# Empty membership
# ---------------------------------------------------------------------------


def test_me_workspaces_empty_when_no_memberships(session: Session) -> None:
    """A user with no workspace bindings gets an empty list (NOT 404)."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset(),  # no memberships
        permissions=frozenset(),
    )
    client = _client_for(session, ctx)

    resp = client.get("/api/v1/me/workspaces")

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Auth required — no override → 401
# ---------------------------------------------------------------------------


def test_me_workspaces_requires_auth(session: Session) -> None:
    """Without a `get_current_user` override, the bearer dependency
    raises 401 — verifies the route is actually wired through the
    auth dep and didn't accidentally become public."""
    client = _client_for(session, ctx=None)

    resp = client.get("/api/v1/me/workspaces")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_me_workspaces_response_shape(session: Session) -> None:
    """Every row has the four fields the frontend `WorkspaceItem`
    consumes: `id` (str), `name`, `isolation_level`, `status`."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=frozenset({"chat:ask"}),
    )
    client = _client_for(session, ctx)

    rows = client.get("/api/v1/me/workspaces").json()
    assert rows, "expected at least one workspace"
    expected_keys = {"id", "name", "isolation_level", "status"}
    for row in rows:
        assert set(row.keys()) == expected_keys
        # id is a stringified UUID (frontend stores as string).
        assert isinstance(row["id"], str)
        uuid.UUID(row["id"])  # round-trips → valid UUID
        assert row["isolation_level"] in {"logical", "physical"}
        assert row["status"] in {"enable", "disable"}


# ---------------------------------------------------------------------------
# Deterministic ordering — by name case-insensitive
# ---------------------------------------------------------------------------


def test_me_workspaces_sorted_by_name(session: Session) -> None:
    """`el-select` dropdown order must be stable across sessions; the
    backend sorts by name (casefold) so the UI doesn't have to."""
    ctx = UserContext(
        user_id=_user_id(session, DEMO_ALICE_USERNAME),
        username=DEMO_ALICE_USERNAME,
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({DEMO_ACME_HQ_ID, DEMO_ACME_RD_ID}),
        permissions=frozenset({"chat:ask"}),
    )
    client = _client_for(session, ctx)

    rows = client.get("/api/v1/me/workspaces").json()
    names = [r["name"] for r in rows]
    assert names == sorted(names, key=str.casefold)


# ---------------------------------------------------------------------------
# End-to-end — POST /auth/login → bearer → GET /me/workspaces
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


def test_login_then_me_workspaces_alice_returns_two_real_workspaces(
    session: Session,
) -> None:
    """Reproduces the exact alice flow that failed in the browser:
    POST /auth/login → bearer → GET /me/workspaces.

    The returned UUIDs must be the real `acme-hq` / `acme-rd` ids
    (NOT the historical `00000000-...` placeholder) so the chat
    request downstream has a valid `workspace_id`.
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

    # 2) /me/workspaces with the bearer.
    me_resp = client.get(
        "/api/v1/me/workspaces",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_resp.status_code == 200, me_resp.text
    rows = me_resp.json()

    # 3) The IDs must be the real seeded workspace UUIDs — these are
    #    exactly the UUIDs that `_resolve_workspace_id` will accept
    #    on the subsequent /chat/query request. If this assertion
    #    passes, the original 403 root cause is fixed.
    assert {r["id"] for r in rows} == {
        str(DEMO_ACME_HQ_ID),
        str(DEMO_ACME_RD_ID),
    }
    assert all(r["id"] != "00000000-0000-0000-0000-000000000000" for r in rows)