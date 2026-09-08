"""Tests for audit buffer orphan resilience (M6 debug fix 2026-09-08).

DESIGN §M6 / T6.x — these tests cover the new flush-buffer
behavior that prevents FK violation dead-loops:

  1. `flush_buffer` runs a pre-flight `SELECT id FROM users WHERE
     id = ANY(:ids)` and NULLs out orphan `user_id`s before the
     bulk INSERT.
  2. On `IntegrityError` (non-orphan), payloads are moved to
     `audit:buffer:dead` instead of being requeued to the main
     buffer (the previous behavior created a dead-loop because
     celery `autoretry_for=(Exception,)` re-raised the same
     broken payload every 2s).
  3. On transient infra errors (`OperationalError`, `RedisError`),
     the previous requeue behavior is preserved — those are
     still transient and recoverable.

Naming follows the project convention (`tests/test_audit_service.py`
prefix; sibling file for the new behavior).
"""
from __future__ import annotations

import dataclasses
import json
import uuid
from unittest.mock import MagicMock, call

import pytest
import redis as redis_lib
import sqlalchemy as sa

from agentic_rag_project.audit import AuditEvent, AuditService
from agentic_rag_project.audit.service import DEFAULT_BUFFER_KEY, DEAD_LETTER_BUFFER_KEY
from agentic_rag_project.db.models.audit import AuditLog


# ---------------------------------------------------------------------------
# Helpers (mirrors test_audit_service.py::_event_to_jsonable)
# ---------------------------------------------------------------------------


def _event_to_jsonable(event: AuditEvent) -> str:
    return json.dumps(
        dataclasses.asdict(event), ensure_ascii=False, default=str
    )


# ---------------------------------------------------------------------------
# Fixtures (mirror test_audit_service.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> MagicMock:
    client = MagicMock(spec=redis_lib.Redis)
    client.lpush = MagicMock(return_value=1)
    client.rpop = MagicMock(return_value=None)
    return client


@pytest.fixture
def fake_session() -> MagicMock:
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    return s


@pytest.fixture
def fake_session_factory(fake_session):
    def _factory():
        return fake_session

    _factory.last_session = fake_session  # type: ignore[attr-defined]
    return _factory


@pytest.fixture
def service(fake_redis, fake_session_factory) -> AuditService:
    return AuditService(
        redis_client=fake_redis,
        session_factory=fake_session_factory,
    )


# ---------------------------------------------------------------------------
# 1. Pre-flight orphan filtering
# ---------------------------------------------------------------------------


def _stub_existing_users(session_mock, existing_uuids: list[str]) -> None:
    """Wire `session.execute` to return the given UUIDs as existing.

    `flush_buffer` calls `session.execute(select(User.id).where(...))`
    for the pre-flight check; this helper makes that return
    `existing_uuids`. Any other UUID in the batch is treated as orphan.

    Returns plain `uuid.UUID` objects so the service's
    `row[0] in {uuid.UUID, ...}` set-membership check works.
    """
    existing = [(uuid.UUID(uid),) for uid in existing_uuids]
    session_mock.execute.return_value.all.return_value = existing


def test_flush_buffer_nulls_orphan_user_id(
    service: AuditService, fake_redis, fake_session_factory
):
    """An orphan `user_id` (UUID not in `users`) must be NULL-ed out.

    Pre-flight check sees the UUID is missing → mapping's `user_id`
    becomes `None`. The bulk INSERT succeeds because the column is
    nullable; the FK is satisfied (None is always allowed).
    """
    orphan_uid = str(uuid.uuid4())
    _stub_existing_users(fake_session_factory.last_session, existing_uuids=[])

    payload = _event_to_jsonable(
        AuditEvent(user_id=orphan_uid, action="query", query="orphan-me")
    )
    fake_redis.rpop.side_effect = [payload.encode("utf-8"), None]

    written = service.flush_buffer(batch_size=10)
    assert written == 1, "orphan row should still be written (user_id=NULL)"

    session = fake_session_factory.last_session
    session.bulk_insert_mappings.assert_called_once()
    mappings = session.bulk_insert_mappings.call_args[0][1]
    assert len(mappings) == 1
    assert mappings[0]["user_id"] is None, "orphan user_id must be NULL"
    assert mappings[0]["action"] == "query"


def test_flush_buffer_keeps_valid_user_id_intact(
    service: AuditService, fake_redis, fake_session_factory
):
    """Valid `user_id` must NOT be NULL-ed out by the pre-flight check."""
    valid_uid = str(uuid.uuid4())
    _stub_existing_users(
        fake_session_factory.last_session, existing_uuids=[valid_uid]
    )

    payload = _event_to_jsonable(
        AuditEvent(user_id=valid_uid, action="query", query="valid-user")
    )
    fake_redis.rpop.side_effect = [payload.encode("utf-8"), None]

    written = service.flush_buffer(batch_size=10)
    assert written == 1

    session = fake_session_factory.last_session
    mappings = session.bulk_insert_mappings.call_args[0][1]
    assert mappings[0]["user_id"] == uuid.UUID(valid_uid)


def test_flush_buffer_handles_mixed_orphan_and_valid_batch(
    service: AuditService, fake_redis, fake_session_factory
):
    """Mixed batch: valid users keep their UUID; orphans become None.

    All rows commit in a single bulk_insert_mappings call. No requeue.
    """
    valid_uid = str(uuid.uuid4())
    orphan_uid_1 = str(uuid.uuid4())
    orphan_uid_2 = str(uuid.uuid4())
    _stub_existing_users(
        fake_session_factory.last_session, existing_uuids=[valid_uid]
    )

    payloads = [
        _event_to_jsonable(
            AuditEvent(user_id=valid_uid, action="query", query="v")
        ),
        _event_to_jsonable(
            AuditEvent(user_id=orphan_uid_1, action="feedback_submit")
        ),
        _event_to_jsonable(
            AuditEvent(user_id=orphan_uid_2, action="csat_read")
        ),
    ]
    fake_redis.rpop.side_effect = [
        p.encode("utf-8") for p in payloads
    ] + [None]

    written = service.flush_buffer(batch_size=10)
    assert written == 3, "all 3 rows must commit (orphans as user_id=NULL)"

    session = fake_session_factory.last_session
    mappings = session.bulk_insert_mappings.call_args[0][1]
    assert len(mappings) == 3
    user_ids = [m["user_id"] for m in mappings]
    assert user_ids[0] == uuid.UUID(valid_uid)
    assert user_ids[1] is None
    assert user_ids[2] is None

    # Pre-flight query must have been issued exactly once.
    session.execute.assert_called_once()
    # CRITICAL: no LPUSH back to the main buffer (that would be the
    # dead-loop behavior we're trying to fix).
    fake_redis.lpush.assert_not_called()


# ---------------------------------------------------------------------------
# 2. IntegrityError → dead-letter (not requeue, not raise)
# ---------------------------------------------------------------------------


def test_flush_buffer_dead_letters_on_integrity_error(
    service: AuditService, fake_redis, fake_session_factory
):
    """When bulk_insert_mappings raises `IntegrityError`, payloads MUST:

    1. Be moved to `audit:buffer:dead` (NOT requeued to main buffer).
    2. NOT raise (the celery task acks success — no autoretry).
    3. NOT be re-LPUSHed to the main buffer under any key.
    """
    _stub_existing_users(fake_session_factory.last_session, existing_uuids=[])

    payload_bytes = _event_to_jsonable(
        AuditEvent(user_id=str(uuid.uuid4()), action="query", query="poison")
    ).encode("utf-8")
    fake_redis.rpop.side_effect = [payload_bytes, None]

    session = fake_session_factory.last_session
    session.bulk_insert_mappings.side_effect = sa.exc.IntegrityError(
        "INSERT", {}, Exception("schema drift")
    )

    # MUST NOT raise — the celery task should treat this as success.
    written = service.flush_buffer(batch_size=10)
    assert written == 0

    # Main buffer must NOT receive the payload back.
    for call_args in fake_redis.lpush.call_args_list:
        assert call_args[0][0] != DEFAULT_BUFFER_KEY, (
            "IntegrityError must NOT requeue to the main buffer"
        )

    # Dead-letter buffer must receive the payload.
    dead_letter_writes = [
        c for c in fake_redis.lpush.call_args_list
        if c[0][0] == DEAD_LETTER_BUFFER_KEY
    ]
    assert len(dead_letter_writes) == 1, (
        "IntegrityError must LPUSH exactly once to audit:buffer:dead"
    )
    # The dead-letter entry wraps the raw payload + a reason envelope.
    dead_value = dead_letter_writes[0][0][1]
    envelope = json.loads(dead_value)
    assert "reason" in envelope
    assert envelope["raw"] == payload_bytes.decode("utf-8")


def test_flush_buffer_still_requeues_on_transient_operational_error(
    service: AuditService, fake_redis, fake_session_factory
):
    """Transient PG errors (`OperationalError`) MUST still requeue.

    The requeue behavior is preserved for infra errors — only
    *logical* errors (`IntegrityError`, schema drift) move to
    dead-letter. This is the regression guard for the dead-loop
    fix: we must not lose the transient-error recovery path.
    """
    payload_bytes = _event_to_jsonable(
        AuditEvent(user_id=str(uuid.uuid4()), action="query", query="transient")
    ).encode("utf-8")
    fake_redis.rpop.side_effect = [payload_bytes, None]

    session = fake_session_factory.last_session
    session.bulk_insert_mappings.side_effect = sa.exc.OperationalError(
        "INSERT", {}, Exception("pg connection lost")
    )

    with pytest.raises(sa.exc.OperationalError):
        service.flush_buffer(batch_size=10)

    # Main buffer must receive the payload back (transient recovery).
    fake_redis.lpush.assert_called_once()
    assert fake_redis.lpush.call_args[0][0] == DEFAULT_BUFFER_KEY
    # Dead-letter MUST NOT be touched.
    for call_args in fake_redis.lpush.call_args_list:
        assert call_args[0][0] != DEAD_LETTER_BUFFER_KEY


# ---------------------------------------------------------------------------
# 3. Defensive: no pre-flight when buffer is empty
# ---------------------------------------------------------------------------


def test_flush_buffer_no_preflight_when_empty(
    service: AuditService, fake_redis, fake_session_factory
):
    """Empty buffer → no pre-flight query, no PG calls, return 0."""
    fake_redis.rpop.return_value = None
    written = service.flush_buffer(batch_size=10)
    assert written == 0
    fake_session_factory.last_session.execute.assert_not_called()
    fake_session_factory.last_session.bulk_insert_mappings.assert_not_called()
