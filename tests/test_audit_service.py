"""Tests for the audit module (M4.3.1).

DESIGN §2.2 #11 — these tests cover the **Decision E** minimum
audit-module contract:
  1. `record()` pushes to the Redis buffer (LPUSH).
  2. `record()` falls back to `_sync_insert()` on Redis error.
  3. `flush_buffer()` RPOPs + bulk-INSERTs into PG.
  4. `query()` returns rows as JSON-friendly dicts with the filters
     applied.
  5. The WORM adapter methods raise `PermissionError` on any accidental
     mutator call (defense in depth — the DB trigger is the
     authoritative layer).

The WORM **trigger** itself is not exercised here because the test
container is SQLite (per TASK M4.3.1.f); trigger semantics are
verified by the 0011 alembic migration + the live dev dry-run.
"""
from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import redis as redis_lib

from agentic_rag_project.audit import AuditEvent, AuditService
from agentic_rag_project.audit.service import DEFAULT_BUFFER_KEY
from agentic_rag_project.db.models.audit import AuditLog


def _event_to_jsonable(event: AuditEvent) -> str:
    """Encode an `AuditEvent` to the same JSON shape `service.record` produces.

    Mirrors `service.record` which uses `dataclasses.asdict(event)` —
    keeping the round-trip predictable for the buffer-flush tests.
    """
    return json.dumps(dataclasses.asdict(event), ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> MagicMock:
    """Return a `MagicMock` shaped like `redis.Redis`.

    `lpush` / `rpop` are stubbed with `MagicMock()` so tests can
    configure side_effects (e.g. raising `redis_lib.RedisError`).
    """
    client = MagicMock(spec=redis_lib.Redis)
    client.lpush = MagicMock(return_value=1)
    client.rpop = MagicMock(return_value=None)  # empty buffer by default
    return client


@pytest.fixture
def fake_session() -> MagicMock:
    """A single shared mock session used by every factory invocation.

    Tests inspect `add.call_args`, `bulk_insert_mappings.call_args`,
    `execute.return_value.scalars.return_value.all.return_value`,
    etc. One session per test (fixture-scoped) keeps assertions simple
    and avoids `sessions[-1]` ordering pitfalls.

    Critically, this fixture wires `__enter__` to return the same
    session object so the `with self._session_factory() as session:`
    pattern in `AuditService` operates on the mock we assert against,
    not a fresh one created by `MagicMock.__enter__`.
    """
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    return s


@pytest.fixture
def fake_session_factory(fake_session):
    """A factory callable that always returns the same mock session.

    The factory function is what `AuditService` calls via
    `self._session_factory()` — returning the shared mock means
    tests can attach `.last_session = fake_session` without worrying
    about invocation order.
    """

    def _factory():
        return fake_session

    _factory.last_session = fake_session  # type: ignore[attr-defined]
    return _factory


@pytest.fixture
def service(fake_redis, fake_session_factory) -> AuditService:
    """Build a default `AuditService` against the fakes above."""
    return AuditService(
        redis_client=fake_redis,
        session_factory=fake_session_factory,
    )


# ---------------------------------------------------------------------------
# 1. record → Redis buffer
# ---------------------------------------------------------------------------


def test_record_pushes_to_redis_buffer(service: AuditService, fake_redis):
    """`record` must LPUSH the JSON-serialized event under the buffer key."""
    event = AuditEvent(
        user_id=str(uuid.uuid4()),
        action="query",
        query="hello world",
        retrieved_doc_ids=["doc-1", "doc-2"],
        model="openai/qwen3.5-plus",
    )
    service.record(event)

    fake_redis.lpush.assert_called_once()
    args, _ = fake_redis.lpush.call_args
    assert args[0] == DEFAULT_BUFFER_KEY
    payload = args[1]
    decoded = json.loads(payload)
    assert decoded["action"] == "query"
    assert decoded["query"] == "hello world"
    assert decoded["retrieved_doc_ids"] == ["doc-1", "doc-2"]
    assert decoded["model"] == "openai/qwen3.5-plus"


def test_record_persists_sanitized_query_in_extra(
    service: AuditService, fake_redis
):
    """`sanitized_query` should travel in the payload (server picks up extra)."""
    event = AuditEvent(
        user_id=str(uuid.uuid4()),
        action="guardrail_block",
        query="contact me at 13800001234",
        sanitized_query="contact me at [PHONE]",
        extra={"block_reason": "sensitive_word"},
    )
    service.record(event)
    decoded = json.loads(fake_redis.lpush.call_args[0][1])
    assert decoded["sanitized_query"] == "contact me at [PHONE]"
    assert decoded["extra"]["block_reason"] == "sensitive_word"


# ---------------------------------------------------------------------------
# 2. record → Redis-down fallback
# ---------------------------------------------------------------------------


def test_record_falls_back_to_sync_insert_on_redis_error(
    service: AuditService, fake_redis, fake_session_factory
):
    """When Redis raises, `record` must call `_sync_insert` (not crash)."""
    fake_redis.lpush.side_effect = redis_lib.ConnectionError("redis down")
    event = AuditEvent(
        user_id=str(uuid.uuid4()),
        action="query",
        query="offline-mode",
    )
    service.record(event)  # must NOT raise

    session = fake_session_factory.last_session
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert isinstance(added, AuditLog)
    assert added.action == "query"
    assert added.query == "offline-mode"
    session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# 3. flush_buffer → bulk INSERT
# ---------------------------------------------------------------------------


def test_flush_buffer_returns_zero_on_empty_buffer(
    service: AuditService, fake_redis
):
    """No events → no PG writes, no errors, return 0.

    RPOP returns None on first hit → loop short-circuits without
    burning the rest of `batch_size` calls. (Re-trying doesn't help
    since the buffer is empty.)
    """
    fake_redis.rpop.return_value = None
    written = service.flush_buffer(batch_size=10)
    assert written == 0
    assert fake_redis.rpop.call_count == 1


def test_flush_buffer_writes_payloads_via_bulk_insert(
    service: AuditService, fake_redis, fake_session_factory
):
    """Happy path: 2 buffered events → 1 bulk_insert_mappings call → 2 rows."""
    e1 = AuditEvent(
        user_id=str(uuid.uuid4()),
        action="query",
        query="q1",
        retrieved_doc_ids=["a"],
        sanitized_query="q1-san",
    )
    e2 = AuditEvent(
        user_id=str(uuid.uuid4()),
        action="feedback_submit",
        extra={"rating": 5},
    )
    fake_redis.rpop.side_effect = [
        _event_to_jsonable(e1).encode("utf-8"),
        _event_to_jsonable(e2).encode("utf-8"),
        None,
    ]
    written = service.flush_buffer(batch_size=10)
    assert written == 2

    session = fake_session_factory.last_session
    session.bulk_insert_mappings.assert_called_once()
    args, _ = session.bulk_insert_mappings.call_args
    assert args[0] is AuditLog
    mappings = args[1]
    assert len(mappings) == 2
    # sanitized_query is moved into extra by the flush path
    assert mappings[0]["extra"]["sanitized_query"] == "q1-san"
    assert mappings[1]["extra"]["rating"] == 5
    session.commit.assert_called_once()


def test_flush_buffer_requeues_on_pg_failure(
    service: AuditService, fake_redis, fake_session_factory
):
    """PG failure must LPUSH the popped payloads back to Redis, then raise."""
    payload_bytes = _event_to_jsonable(
        AuditEvent(
            user_id=str(uuid.uuid4()), action="query", query="requeue-me"
        )
    ).encode("utf-8")
    fake_redis.rpop.side_effect = [payload_bytes, None]

    session = fake_session_factory.last_session
    session.bulk_insert_mappings.side_effect = RuntimeError("pg down")

    with pytest.raises(RuntimeError):
        service.flush_buffer(batch_size=10)

    # Payload must be LPUSHed back to the head of the buffer.
    fake_redis.lpush.assert_called_once()
    assert fake_redis.lpush.call_args[0][0] == DEFAULT_BUFFER_KEY


# ---------------------------------------------------------------------------
# 4. query → admin read path
# ---------------------------------------------------------------------------


def test_query_returns_serialized_rows(service, fake_session_factory):
    """`query` should hand back rows as JSON-friendly dicts."""
    user_id = uuid.uuid4()
    ts = datetime(2026, 8, 26, 12, 0, 0)

    fake_row = MagicMock(spec=AuditLog)
    fake_row.id = uuid.uuid4()
    fake_row.ts = ts
    fake_row.user_id = user_id
    fake_row.workspace_id = None
    fake_row.action = "query"
    fake_row.query = "show me X"
    fake_row.retrieved_docs = [{"doc_id": "d1"}, {"doc_id": "d2"}]
    fake_row.model = "openai/qwen3.5-plus"
    fake_row.extra = {"sanitized_query": "show me Y"}

    session = fake_session_factory.last_session
    session.execute.return_value.scalars.return_value.all.return_value = [
        fake_row
    ]

    rows = service.query(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "query"
    assert row["query"] == "show me X"
    assert row["sanitized_query"] == "show me Y"
    assert row["retrieved_doc_ids"] == ["d1", "d2"]
    assert row["user_id"] == str(user_id)
    assert row["ts"] == ts.isoformat()


def test_query_applies_filters(service, fake_session_factory):
    """When filters are set, the SQL must include WHERE clauses."""
    session = fake_session_factory.last_session
    session.execute.return_value.scalars.return_value.all.return_value = []

    service.query(
        user_id=str(uuid.uuid4()),
        action="guardrail_block",
        ts_from=datetime(2026, 8, 1),
        ts_to=datetime(2026, 8, 26),
        limit=50,
        offset=10,
    )
    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_logs" in compiled
    assert "action" in compiled
    assert "user_id" in compiled
    assert "ts" in compiled
    assert "LIMIT 50" in compiled or "limit" in compiled.lower()
    assert "OFFSET 10" in compiled or "offset" in compiled.lower()


# ---------------------------------------------------------------------------
# 5. WORM adapter — accidental mutators fast-fail
# ---------------------------------------------------------------------------


def test_immutable_update_raises(service: AuditService):
    """`_immutable_update` must raise PermissionError to surface dev-time bugs."""
    with pytest.raises(PermissionError, match="WORM"):
        service._immutable_update()


def test_immutable_delete_raises(service: AuditService):
    """`_immutable_delete` must raise PermissionError (DB trigger is the real guard)."""
    with pytest.raises(PermissionError, match="WORM"):
        service._immutable_delete()
