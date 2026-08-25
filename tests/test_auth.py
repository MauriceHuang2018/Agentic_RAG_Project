"""Tests for the API gateway auth middleware (T5.1).

Covers:
  - JWT round-trip (create -> decode -> payload integrity)
  - Password hashing (bcrypt)
  - FastAPI dependency: missing / invalid / expired tokens
  - `require_super_admin` blocks non-admins
  - `require_permission` factory enforces the right key
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_current_user,
    require_permission,
    require_super_admin,
)
from agentic_rag_project.api_gateway.jwt import (
    TokenError,
    create_access_token,
    decode_access_token,
)
from agentic_rag_project.api_gateway.router import hash_password, verify_password
from agentic_rag_project.config import get_settings


# --- JWT round-trip ---


def test_create_and_decode_token_round_trip() -> None:
    """`create_access_token` produces a token that decodes to the same subject."""
    user_id = uuid.uuid4()
    token = create_access_token(user_id, extra_claims={"is_super_admin": True})
    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["is_super_admin"] is True


def test_decode_raises_on_invalid_signature() -> None:
    """Tokens signed with a different secret are rejected."""
    settings = get_settings()
    bad = jwt.encode({"sub": "x"}, "different-secret", algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError):
        decode_access_token(bad)


def test_decode_raises_on_expired_token() -> None:
    """Expired tokens raise TokenError."""
    settings = get_settings()
    expired = datetime.now(timezone.utc) - timedelta(minutes=10)
    payload = {
        "sub": str(uuid.uuid4()),
        "exp": int(expired.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError):
        decode_access_token(token)


# --- Password hashing ---


def test_password_hash_is_not_plaintext() -> None:
    """`hash_password` returns a bcrypt hash (starts with $2)."""
    hashed = hash_password("hunter2")
    assert hashed != "hunter2"
    assert hashed.startswith("$2")


def test_password_verify_round_trip() -> None:
    """`verify_password` returns True for correct password, False for wrong one."""
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)


# --- get_current_user dependency ---


def _make_app_with_protected_route(ctx_provider) -> FastAPI:
    """Build a minimal app that mounts a single protected route."""
    app = FastAPI()

    @app.get("/protected")
    async def protected(
        ctx: UserContext = Depends(ctx_provider),
    ) -> dict:
        return {"user_id": str(ctx.user_id), "is_super_admin": ctx.is_super_admin}

    return app


def test_get_current_user_requires_bearer_token() -> None:
    """No Authorization header -> 401 with `missing_bearer_token` detail."""
    from fastapi.testclient import TestClient

    app = _make_app_with_protected_route(get_current_user)
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing_bearer_token"


def test_get_current_user_rejects_malformed_token() -> None:
    """A garbage bearer token -> 401, not 500."""
    from fastapi.testclient import TestClient

    app = _make_app_with_protected_route(get_current_user)
    client = TestClient(app)
    response = client.get(
        "/protected", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert response.status_code == 401


def test_require_super_admin_blocks_non_admin() -> None:
    """`require_super_admin` rejects requests where the user is not admin.

    Uses a stub dependency so we exercise the gating logic without a DB.
    """
    from fastapi.testclient import TestClient

    app = FastAPI()

    def _stub_ctx() -> UserContext:
        return UserContext(
            user_id=uuid.uuid4(),
            username="alice",
            is_super_admin=False,
            status="enable",
            workspace_ids=frozenset(),
            permissions=frozenset(),
        )

    @app.get("/admin")
    async def admin(
        ctx: UserContext = Depends(require_super_admin),
    ) -> dict:
        return {"user_id": str(ctx.user_id)}

    app.dependency_overrides[get_current_user] = _stub_ctx
    client = TestClient(app)
    response = client.get("/admin")
    assert response.status_code == 403
    assert response.json()["detail"] == "super_admin_required"


def test_require_permission_factory_allows_matching_key() -> None:
    """`require_permission("kb:read")` passes when ctx.permissions contains it."""
    from fastapi.testclient import TestClient

    app = FastAPI()

    def _stub_ctx() -> UserContext:
        return UserContext(
            user_id=uuid.uuid4(),
            username="bob",
            is_super_admin=False,
            status="enable",
            workspace_ids=frozenset(),
            permissions=frozenset({"kb:read", "doc:read"}),
        )

    @app.get("/kb")
    async def kb(
        ctx: UserContext = Depends(require_permission("kb:read")),
    ) -> dict:
        return {"ok": True}

    app.dependency_overrides[get_current_user] = _stub_ctx
    client = TestClient(app)
    assert client.get("/kb").status_code == 200


def test_require_permission_factory_blocks_missing_key() -> None:
    """`require_permission("admin:audit")` rejects when the key is absent."""
    from fastapi.testclient import TestClient

    app = FastAPI()

    def _stub_ctx() -> UserContext:
        return UserContext(
            user_id=uuid.uuid4(),
            username="carol",
            is_super_admin=False,
            status="enable",
            workspace_ids=frozenset(),
            permissions=frozenset({"doc:read"}),
        )

    @app.get("/audit")
    async def audit(
        ctx: UserContext = Depends(require_permission("admin:audit")),
    ) -> dict:
        return {"ok": True}

    app.dependency_overrides[get_current_user] = _stub_ctx
    client = TestClient(app)
    response = client.get("/audit")
    assert response.status_code == 403
    assert "admin:audit" in response.json()["detail"]


def test_require_permission_factory_wildcard_short_circuits() -> None:
    """Super-admin's '*' permission short-circuits all permission checks."""
    from fastapi.testclient import TestClient

    app = FastAPI()

    def _stub_ctx() -> UserContext:
        return UserContext(
            user_id=uuid.uuid4(),
            username="root",
            is_super_admin=True,
            status="enable",
            workspace_ids=frozenset(),
            permissions=frozenset({"*"}),
        )

    @app.get("/anything")
    async def anything(
        ctx: UserContext = Depends(require_permission("any:random:key")),
    ) -> dict:
        return {"ok": True}

    app.dependency_overrides[get_current_user] = _stub_ctx
    client = TestClient(app)
    assert client.get("/anything").status_code == 200