"""Tests for M5 T5 — three new AuditAction literals + their audit hooks.

DESIGN §4.6.2 / TASK T5 — the audit whitelist grew from 8 to 11
literals:

  * `csat_read`              — admin calls /admin/csat/* (M4.4 TODO #5)
  * `role_bind`              — RBAC role-binding mutation (co-exists
                                with legacy `role_assign`)
  * `sensitive_word_update`  — sensitive word hot-reload

These tests pin:

  1. The AuditAction Literal type itself contains the 3 new values
     (regression guard for T1; ensures we never silently drop a
     literal that the DB CHECK now accepts).
  2. Each of the 3 admin/CSAT endpoints emits exactly one
     `csat_read` audit row when called through the router with a
     minimal app. We pin the `extra` payload so reviewers can see
     *which* endpoint triggered the read.
  3. `rbac.assignments.assign_role(..., audit_service=...)` emits a
     `role_bind` row that names the *actor* (admin), not the
     subject (user receiving the role).
  4. `post_processor.sensitive_sync(..., audit_service=...)` emits a
     `sensitive_word_update` row only when the in-process filter
     was actually rebuilt AND an actor_id is supplied.
  5. The pre-existing `role_assign` literal is still accepted by the
     whitelist (no silent drift — the 11-value whitelist is
     backward-compatible with the historical literal).

The CSAT tests use FastAPI's `TestClient` with the same dependency
overrides pattern as `test_feedback_authorization.py` / T2: a
fresh app, an in-memory SQLite engine, and a `UserContext` with the
relevant `csat:read` permission.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agentic_rag_project.api_gateway import admin_router
from agentic_rag_project.api_gateway.dependencies import (
    UserContext,
    get_audit_service,
    get_current_user,
)
from agentic_rag_project.audit import AuditEvent, AuditService
from agentic_rag_project.audit.events import AuditAction
from agentic_rag_project.db.session import get_db
from agentic_rag_project.db.models import (
    Role,
    SensitiveValue,
    User,
    UserRole,
    Workspace,
)
from agentic_rag_project.db.models.base import Base as _Base
from agentic_rag_project.post_processor.filter import (
    SENSITIVE_WORDS_REDIS_KEY,
    SensitiveWordFilter,
)
from agentic_rag_project.post_processor.sensitive_sync import sensitive_sync
from agentic_rag_project.rbac.assignments import assign_role


# ---------------------------------------------------------------------------
# Part 1 — AuditAction Literal type itself
# ---------------------------------------------------------------------------


def test_t5_audit_action_literal_includes_new_values() -> None:
    """The 11-value whitelist must include the 3 M5 additions.

    This is the cheapest possible regression guard: a typo in
    `events.py` (or someone removing a literal by accident) flips
    this test red before the DB CHECK ever sees the wrong value.
    """
    literals = set(AuditAction.__args__)
    expected = {
        # 8 historical
        "query",
        "ingest",
        "delete",
        "access_denied",
        "guardrail_block",
        "feedback_submit",
        "role_assign",
        "sensitive_update",
        # 3 new (M5 T1/T5)
        "csat_read",
        "role_bind",
        "sensitive_word_update",
    }
    assert literals == expected, (
        f"AuditAction whitelist drifted.\n"
        f"  missing: {expected - literals}\n"
        f"  extra:   {literals - expected}"
    )


def test_t5_audit_action_legacy_role_assign_still_accepted() -> None:
    """`role_assign` is a legacy literal still used by historical
    audit rows. The whitelist must keep accepting it so old data
    doesn't suddenly become invalid (the DB CHECK is enforced on
    INSERT, not on read — but a future backfill migration would
    break)."""
    assert "role_assign" in AuditAction.__args__


# ---------------------------------------------------------------------------
# Part 2 — admin_router CSAT endpoints emit `csat_read`
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """SQLite in-memory engine. Only the `audit_logs` table is needed
    here — we don't actually query it, we just need the ORM to be
    able to flush the audit row's INSERT through the WORM-shaped
    CHECK constraint. We use the production `_Base.metadata` so
    every table is available; the SQLite engine skips the
    PG-only types via the type-compiler.
    """
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False)


def _build_csat_app(
    *,
    user_ctx: UserContext,
    audit_service_stub: MagicMock,
    session_factory,
) -> FastAPI:
    """Wire `/admin/csat/*` to a fresh app with dependency overrides."""
    app = FastAPI()
    app.include_router(admin_router.get_router())
    app.dependency_overrides[get_current_user] = lambda: user_ctx
    app.dependency_overrides[get_audit_service] = lambda: audit_service_stub

    def _fake_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _fake_db
    return app


@pytest.fixture
def audit_service_stub() -> MagicMock:
    svc = MagicMock(spec=AuditService)
    svc.record = MagicMock()
    return svc


@pytest.fixture
def admin_ctx() -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        username="ops",
        is_super_admin=False,
        status="enable",
        workspace_ids=frozenset({uuid.uuid4()}),
        permissions=frozenset({"csat:read"}),
    )


def _records(audit_service_stub: MagicMock) -> list[AuditEvent]:
    """Pull every `AuditEvent` the stub captured, in call order."""
    return [c.args[0] for c in audit_service_stub.record.call_args_list]


def test_t5_csat_summary_emits_csat_read_audit(
    engine,
    session_factory,
    audit_service_stub: MagicMock,
    admin_ctx: UserContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GET /admin/csat/summary` must emit exactly one `csat_read`
    audit row, with `extra.endpoint == 'summary'`.
    """
    # The summary endpoint calls `compute_csat_summary` which would
    # need real PG tables. We patch the module-level helper to
    # return a known shape so the test stays engine-agnostic.
    sentinel_summary = _mk_summary_stub(next(iter(admin_ctx.workspace_ids)))
    monkeypatch.setattr(
        admin_router,
        "compute_csat_summary",
        lambda session, workspace_id, window_days: sentinel_summary,
    )

    app = _build_csat_app(
        user_ctx=admin_ctx,
        audit_service_stub=audit_service_stub,
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/admin/csat/summary?workspace_id={sentinel_summary.workspace_id}&window_days=7"
        )

    assert resp.status_code == 200, resp.text
    events = _records(audit_service_stub)
    csat_events = [e for e in events if e.action == "csat_read"]
    assert len(csat_events) == 1
    assert csat_events[0].extra["endpoint"] == "summary"
    assert csat_events[0].extra["window_days"] == 7


def test_t5_csat_timeseries_emits_csat_read_audit(
    engine,
    session_factory,
    audit_service_stub: MagicMock,
    admin_ctx: UserContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GET /admin/csat/timeseries` must emit exactly one `csat_read`
    audit row, with `extra.endpoint == 'timeseries'` and the
    bucket recorded.
    """
    ws_id = next(iter(admin_ctx.workspace_ids))
    sentinel_ts = _mk_timeseries_stub(ws_id)
    monkeypatch.setattr(
        admin_router,
        "compute_csat_timeseries",
        lambda session, workspace_id, window_days, bucket: sentinel_ts,
    )

    app = _build_csat_app(
        user_ctx=admin_ctx,
        audit_service_stub=audit_service_stub,
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/admin/csat/timeseries?workspace_id={ws_id}&window_days=30&bucket=hour"
        )

    assert resp.status_code == 200, resp.text
    events = _records(audit_service_stub)
    csat_events = [e for e in events if e.action == "csat_read"]
    assert len(csat_events) == 1
    assert csat_events[0].extra["endpoint"] == "timeseries"
    assert csat_events[0].extra["bucket"] == "hour"
    assert csat_events[0].extra["window_days"] == 30


def test_t5_csat_by_category_emits_csat_read_audit(
    engine,
    session_factory,
    audit_service_stub: MagicMock,
    admin_ctx: UserContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GET /admin/csat/by-category` must emit exactly one `csat_read`
    audit row with `extra.endpoint == 'by_category'`.
    """
    ws_id = next(iter(admin_ctx.workspace_ids))
    sentinel_cat = _mk_by_category_stub(ws_id)
    monkeypatch.setattr(
        admin_router,
        "compute_csat_by_category",
        lambda session, workspace_id, window_days: sentinel_cat,
    )

    app = _build_csat_app(
        user_ctx=admin_ctx,
        audit_service_stub=audit_service_stub,
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/admin/csat/by-category?workspace_id={ws_id}&window_days=1"
        )

    assert resp.status_code == 200, resp.text
    events = _records(audit_service_stub)
    csat_events = [e for e in events if e.action == "csat_read"]
    assert len(csat_events) == 1
    assert csat_events[0].extra["endpoint"] == "by_category"


# ---------------------------------------------------------------------------
# Part 3 — rbac.assignments.assign_role emits `role_bind`
# ---------------------------------------------------------------------------


def test_t5_assign_role_emits_role_bind_audit(
    engine,
    session_factory,
) -> None:
    """When `assign_role()` is called with an `audit_service`, a
    `role_bind` audit row is staged AFTER the binding commits, with
    `user_id == granted_by` (the admin) — NOT the subject user.
    """
    # We need a User, Role, and Workspace to exist. Minimal columns:
    # Use `_Base.metadata.create_all` against SQLite which will
    # honour the simplified types.
    _Base.metadata.create_all(engine)
    user_id = uuid.uuid4()
    role_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    _seed_min_rbac(
        session_factory,
        user_id=user_id,
        username="subject",
        role_id=role_id,
        role_name="operator",
        workspace_id=ws_id,
        workspace_name="ws-A",
    )

    audit_svc = MagicMock(spec=AuditService)
    audit_svc.record = MagicMock()

    with session_factory() as s:
        binding = assign_role(
            s,
            user_id=user_id,
            role_id=role_id,
            workspace_id=ws_id,
            granted_by=actor_id,
            audit_service=audit_svc,
        )
        assert binding.user_id == user_id

    audit_calls = [
        c.args[0] for c in audit_svc.record.call_args_list
    ]
    assert len(audit_calls) == 1
    evt = audit_calls[0]
    assert evt.action == "role_bind"
    # The actor (admin) is the audit `user_id`, not the subject.
    assert evt.user_id == str(actor_id)
    assert evt.extra["subject_user_id"] == str(user_id)
    assert evt.extra["role_id"] == str(role_id)
    assert evt.extra["role_name"] == "operator"
    assert evt.extra["workspace_id"] == str(ws_id)


def test_t5_assign_role_without_audit_service_does_not_emit(
    engine,
    session_factory,
) -> None:
    """Calling `assign_role()` without an `audit_service` (the
    default None) keeps existing behaviour: no audit row, no error.
    Seed scripts and tests rely on this contract.
    """
    _Base.metadata.create_all(engine)
    user_id = uuid.uuid4()
    role_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    _seed_min_rbac(
        session_factory,
        user_id=user_id,
        username="subject",
        role_id=role_id,
        role_name="operator",
        workspace_id=ws_id,
        workspace_name="ws-A",
    )

    with session_factory() as s:
        binding = assign_role(
            s,
            user_id=user_id,
            role_id=role_id,
            workspace_id=ws_id,
            granted_by=uuid.uuid4(),
            # No audit_service kwarg — default None.
        )
        assert binding is not None


def test_t5_assign_role_no_actor_skips_audit(
    engine,
    session_factory,
) -> None:
    """When `granted_by is None` (system-initiated binding, e.g.
    migration bootstrap), skip the `role_bind` audit even if an
    `audit_service` is supplied — we don't have an actor to
    attribute the change to.
    """
    _Base.metadata.create_all(engine)
    user_id = uuid.uuid4()
    role_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    _seed_min_rbac(
        session_factory,
        user_id=user_id,
        username="subject",
        role_id=role_id,
        role_name="operator",
        workspace_id=ws_id,
        workspace_name="ws-A",
    )

    audit_svc = MagicMock(spec=AuditService)
    audit_svc.record = MagicMock()

    with session_factory() as s:
        assign_role(
            s,
            user_id=user_id,
            role_id=role_id,
            workspace_id=ws_id,
            granted_by=None,  # system-initiated
            audit_service=audit_svc,
        )

    # No actor → no audit row.
    assert audit_svc.record.call_count == 0


# ---------------------------------------------------------------------------
# Part 4 — post_processor.sensitive_sync emits `sensitive_word_update`
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Tiny in-memory Redis stand-in for the sync test."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str | bytes) -> bool:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        self.store[key] = value
        return True

    def get(self, key: str) -> bytes | None:
        val = self.store.get(key)
        if val is None:
            return None
        return val.encode("utf-8")


def test_t5_sensitive_sync_emits_word_update_audit(
    engine,
    session_factory,
) -> None:
    """When `sensitive_sync()` is called with an `audit_service` and
    an `actor_id`, AND the in-process trie is actually rebuilt, an
    audit row tagged `sensitive_word_update` is staged.
    """
    _Base.metadata.create_all(engine)
    actor = uuid.uuid4()

    # Seed one active sensitive_value row so the sync has work to do.
    from agentic_rag_project.db.models import SensitiveValue

    with session_factory() as s:
        s.add(
            SensitiveValue(
                word="alpha",
                is_active=True,
                is_preset=True,
                added_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    # Use a fresh filter so we control its starting state.
    flt = SensitiveWordFilter(words=())
    fake_redis = _FakeRedis()

    audit_svc = MagicMock(spec=AuditService)
    audit_svc.record = MagicMock()

    with session_factory() as s:
        result = sensitive_sync(
            s,
            actor_id=actor,
            action="manual",
            redis_client=fake_redis,
            flt=flt,
            audit_service=audit_svc,
        )

    assert result.filter_replaced is True
    audit_calls = [c.args[0] for c in audit_svc.record.call_args_list]
    word_update = [e for e in audit_calls if e.action == "sensitive_word_update"]
    assert len(word_update) == 1
    evt = word_update[0]
    assert evt.user_id == str(actor)
    assert evt.extra["word_count"] >= 1
    assert evt.extra["redis_ok"] is True
    assert evt.extra["sync_action"] == "manual"


def test_t5_sensitive_sync_no_actor_skips_audit(
    engine,
    session_factory,
) -> None:
    """`sensitive_sync(actor_id=None)` (migration bootstrap) skips
    the `sensitive_word_update` audit even if `audit_service` is
    provided — there's no actor to attribute the change to.
    """
    _Base.metadata.create_all(engine)
    from agentic_rag_project.db.models import SensitiveValue

    with session_factory() as s:
        s.add(
            SensitiveValue(
                word="beta",
                is_active=True,
                is_preset=True,
                added_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    flt = SensitiveWordFilter(words=())
    audit_svc = MagicMock(spec=AuditService)
    audit_svc.record = MagicMock()

    with session_factory() as s:
        sensitive_sync(
            s,
            actor_id=None,  # migration bootstrap
            action="after_write",
            redis_client=_FakeRedis(),
            flt=flt,
            audit_service=audit_svc,
        )

    assert audit_svc.record.call_count == 0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _SummaryStub:
    """Duck-typed CSAT summary shape returned by the patched
    `compute_csat_summary`. The route only reads these fields, so a
    bare object with the right attributes is enough.
    """

    workspace_id: uuid.UUID
    window_days: int
    like_count: int
    dislike_count: int
    total: int
    csat_score: float


def _mk_summary_stub(ws_id: uuid.UUID) -> _SummaryStub:
    s = _SummaryStub()
    s.workspace_id = ws_id
    s.window_days = 7
    s.like_count = 0
    s.dislike_count = 0
    s.total = 0
    s.csat_score = 0.0
    return s


class _TimeseriesStub:
    workspace_id: uuid.UUID
    window_days: int
    bucket: str
    points: list[Any]


def _mk_timeseries_stub(ws_id: uuid.UUID) -> _TimeseriesStub:
    s = _TimeseriesStub()
    s.workspace_id = ws_id
    s.window_days = 30
    s.bucket = "hour"
    s.points = []
    return s


class _ByCategoryStub:
    workspace_id: uuid.UUID
    window_days: int
    categories: list[Any]


def _mk_by_category_stub(ws_id: uuid.UUID) -> _ByCategoryStub:
    s = _ByCategoryStub()
    s.workspace_id = ws_id
    s.window_days = 1
    s.categories = []
    return s


def _seed_min_rbac(
    session_factory,
    *,
    user_id: uuid.UUID,
    username: str,
    role_id: uuid.UUID,
    role_name: str,
    workspace_id: uuid.UUID,
    workspace_name: str,
) -> None:
    """Insert the minimum rows `assign_role()` needs to succeed.

    The full schema is heavy — User has many columns, Role has
    permission JSON, Workspace has status, etc. We insert the bare
    minimum: enabled status + the columns `assign_role()` reads.
    """
    from agentic_rag_project.db.models import Role, User, UserRole, Workspace

    with session_factory() as s:
        s.add(
            User(
                id=user_id,
                username=username,
                email=f"{username}@example.com",
                password_hash="x" * 60,
                status="enable",
                attributes={},
            )
        )
        s.add(
            Role(
                id=role_id,
                name=role_name,
                status="enable",
            )
        )
        s.add(
            Workspace(
                id=workspace_id,
                name=workspace_name,
                owner_id=user_id,
                status="enable",
                isolation_level="logical",
                config={},
            )
        )
        s.commit()


__all__ = [
    "test_t5_audit_action_literal_includes_new_values",
    "test_t5_audit_action_legacy_role_assign_still_accepted",
    "test_t5_csat_summary_emits_csat_read_audit",
    "test_t5_csat_timeseries_emits_csat_read_audit",
    "test_t5_csat_by_category_emits_csat_read_audit",
    "test_t5_assign_role_emits_role_bind_audit",
    "test_t5_assign_role_without_audit_service_does_not_emit",
    "test_t5_assign_role_no_actor_skips_audit",
    "test_t5_sensitive_sync_emits_word_update_audit",
    "test_t5_sensitive_sync_no_actor_skips_audit",
]