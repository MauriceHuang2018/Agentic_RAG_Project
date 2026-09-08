"""Tests for the CSAT admin endpoints (M4.4).

Three API-layer tests cover the auth + validation contract on
`/api/v1/admin/csat/{summary,timeseries,by-category}`:

  1. Happy path — non-super-admin member sees 200 with proper JSON.
  2. Auth boundary — non-member sees 403 (`not_a_member_of_workspace`).
  3. Validation — invalid `bucket` value is rejected by FastAPI
     with 422 (the `pattern=r"^(hour|day)$"` regex).

We mock `compute_csat_*` at the `admin_router` import site because
the helpers are already exercised end-to-end by
`tests/test_csat_queries.py`. This file is purely about the HTTP
shape: routing, RBAC, query-param validation.

The `Session` dependency is overridden with a `MagicMock` — the
endpoints don't actually call the DB session (all work is
delegated to the `compute_csat_*` helpers), but FastAPI still
resolves `get_db` and a real session would slow the test down.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def member_ws_id() -> uuid.UUID:
    """The workspace the fake caller IS a member of."""
    return uuid.uuid4()


@pytest.fixture
def other_ws_id() -> uuid.UUID:
    """The workspace the fake caller is NOT a member of."""
    return uuid.uuid4()


@pytest.fixture
def fake_session() -> MagicMock:
    """A mock Session — admin endpoints don't actually use it."""
    return MagicMock()


@pytest.fixture
def client(member_ws_id: uuid.UUID, fake_session: MagicMock):
    """FastAPI TestClient with `get_db` and `get_current_user` overridden.

    `compute_csat_*` helpers are stubbed at the admin_router import
    site so we can assert the routing + validation contract
    without hitting the DB or the helpers (both are covered
    separately).
    """
    from agentic_rag_project import main as app_main
    from agentic_rag_project.api_gateway import admin_router as admin_mod
    from agentic_rag_project.api_gateway.dependencies import (
        UserContext,
        get_current_user,
    )
    from agentic_rag_project.db import session as db_session

    ctx = UserContext(
        user_id=uuid.uuid4(),
        username="alice",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({member_ws_id}),
        permissions=frozenset({"csat:read"}),
    )

    def _fake_current_user() -> UserContext:
        return ctx

    def _fake_get_db():
        yield fake_session

    app_main.app.dependency_overrides[get_current_user] = _fake_current_user
    app_main.app.dependency_overrides[db_session.get_db] = _fake_get_db

    # Stub the helper to return a deterministic response shape.
    # Each test sets the return value per endpoint.
    stub_summary_data = MagicMock()
    stub_summary_data.workspace_id = member_ws_id
    stub_summary_data.window_days = 7
    stub_summary_data.like_count = 0
    stub_summary_data.dislike_count = 0
    stub_summary_data.total = 0
    stub_summary_data.csat_score = 0.0
    admin_mod.compute_csat_summary = MagicMock(return_value=stub_summary_data)

    stub_timeseries_data = MagicMock()
    stub_timeseries_data.workspace_id = member_ws_id
    stub_timeseries_data.window_days = 7
    stub_timeseries_data.bucket = "day"
    stub_timeseries_data.points = []
    admin_mod.compute_csat_timeseries = MagicMock(return_value=stub_timeseries_data)

    stub_by_category_data = MagicMock()
    stub_by_category_data.workspace_id = member_ws_id
    stub_by_category_data.window_days = 7
    stub_by_category_data.categories = []
    admin_mod.compute_csat_by_category = MagicMock(
        return_value=stub_by_category_data,
    )

    yield TestClient(app_main.app)

    app_main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. happy path
# ---------------------------------------------------------------------------


def test_csat_summary_returns_200_for_member(
    client: TestClient,
    member_ws_id: uuid.UUID,
) -> None:
    """A member calling /csat/summary with their workspace sees 200."""
    response = client.get(
        "/api/v1/admin/csat/summary",
        params={"workspace_id": str(member_ws_id)},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["workspace_id"] == str(member_ws_id)
    assert body["window_days"] == 7
    assert body["like_count"] == 0
    assert body["dislike_count"] == 0
    assert body["total"] == 0
    assert body["csat_score"] == 0.0
    assert "generated_at" in body


def test_csat_timeseries_returns_200_for_member(
    client: TestClient,
    member_ws_id: uuid.UUID,
) -> None:
    """A member calling /csat/timeseries with bucket=hour sees 200."""
    response = client.get(
        "/api/v1/admin/csat/timeseries",
        params={"workspace_id": str(member_ws_id), "bucket": "hour"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_id"] == str(member_ws_id)
    assert body["window_days"] == 7
    assert body["bucket"] == "day"  # stub returned the default value
    assert body["points"] == []


def test_csat_by_category_returns_200_for_member(
    client: TestClient,
    member_ws_id: uuid.UUID,
) -> None:
    """A member calling /csat/by-category sees 200."""
    response = client.get(
        "/api/v1/admin/csat/by-category",
        params={"workspace_id": str(member_ws_id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_id"] == str(member_ws_id)
    assert body["categories"] == []


# ---------------------------------------------------------------------------
# 2. non-member → 403
# ---------------------------------------------------------------------------


def test_csat_summary_returns_403_for_non_member(
    client: TestClient,
    other_ws_id: uuid.UUID,
) -> None:
    """Calling /csat/summary for a workspace the user isn't in → 403."""
    response = client.get(
        "/api/v1/admin/csat/summary",
        params={"workspace_id": str(other_ws_id)},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "not_a_member_of_workspace"


# ---------------------------------------------------------------------------
# 3. invalid bucket → 422
# ---------------------------------------------------------------------------


def test_csat_timeseries_rejects_invalid_bucket_with_422(
    client: TestClient,
    member_ws_id: uuid.UUID,
) -> None:
    """`bucket=week` fails the `pattern=r'^(hour|day)$'` and is rejected."""
    response = client.get(
        "/api/v1/admin/csat/timeseries",
        params={"workspace_id": str(member_ws_id), "bucket": "week"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    # FastAPI's validation error shape includes the failing location
    # so consumers can route on it.
    assert any(
        "bucket" in err.get("loc", [])
        for err in body.get("detail", [])
    )