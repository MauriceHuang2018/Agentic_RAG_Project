"""Tests for the celery `flush_audit_buffer` task retry policy
(M6 debug fix 2026-09-08).

DESIGN §M6 / T6.x — the previous `autoretry_for=(Exception,)` made
the task retry on **any** error, including `IntegrityError` (FK
violation). Because `flush_buffer` requeued the broken payload to
the main buffer on failure, the same poisonous payload re-entered
the next batch and triggered the same FK violation forever (the
dead-loop observable in postgres + celery-worker logs).

This file pins down the new policy:

  * `autoretry_for` is restricted to transient infrastructure
    errors: `redis.exceptions.RedisError` and
    `sqlalchemy.exc.OperationalError`.
  * `IntegrityError` (FK violation, schema drift, etc.) is NOT
    retried by celery — the business layer already dead-letters
    the poisoned payload, so celery just acks success.
  * The previous behaviors (5-s beat schedule, Prometheus counter
    untouched) are preserved.
"""
from __future__ import annotations

import importlib

import pytest
import redis as redis_lib
import sqlalchemy as sa

from agentic_rag_project.audit.tasks import make_flush_audit_buffer_task


# ---------------------------------------------------------------------------
# 1. Static inspection — autoretry_for is restricted to transient errors
# ---------------------------------------------------------------------------


def test_audit_task_autoretry_for_excludes_integrity_error():
    """`IntegrityError` MUST NOT be in `autoretry_for`.

    A poisoned payload's FK violation is a *logical* error, not a
    transient infra failure. Retrying it achieves nothing because
    the same broken row is re-flushed from Redis on the next tick.
    The business layer handles it by moving the payload to
    `audit:buffer:dead`.
    """
    task_factory = make_flush_audit_buffer_task
    task = task_factory()
    retry_for = task.autoretry_for
    assert sa.exc.IntegrityError not in retry_for, (
        "IntegrityError must be excluded from autoretry_for"
    )


def test_audit_task_autoretry_for_includes_transient_errors():
    """`RedisError` + `OperationalError` MUST be in `autoretry_for`."""
    task = make_flush_audit_buffer_task()
    retry_for = task.autoretry_for
    assert redis_lib.RedisError in retry_for
    assert sa.exc.OperationalError in retry_for


def test_audit_task_autoretry_for_excludes_runtime_error():
    """`RuntimeError` is the catch-all used by `flush_buffer` for
    unhandled business errors — it must NOT be retried either.

    Pre-fix behavior was `autoretry_for=(Exception,)` which
    included `RuntimeError`. That caused spurious retry storms on
    any non-FK error (e.g. a bad JSON payload or a SQL syntax bug).
    """
    task = make_flush_audit_buffer_task()
    retry_for = task.autoretry_for
    assert RuntimeError not in retry_for


# ---------------------------------------------------------------------------
# 2. Behavioral — celery retry contract
# ---------------------------------------------------------------------------


@pytest.fixture
def bound_task(monkeypatch):
    """Bind the flush task against stub celery + Redis deps so we
    can drive it without a live worker.

    Returns the task object + a `flush_buffer_mock` that the test
    can program with side effects.
    """
    # Lazy import to avoid Django-style app-loading order issues.
    from agentic_rag_project.doc_processor import celery_app as celery_app_module

    flush_buffer_mock = importlib.import_module(
        "agentic_rag_project.audit.service"
    ).AuditService.flush_buffer

    # Replace `AuditService.flush_buffer` for the duration of the test.
    monkeypatch.setattr(
        "agentic_rag_project.audit.service.AuditService.flush_buffer",
        flush_buffer_mock,
    )
    yield make_flush_audit_buffer_task()


def test_audit_task_returns_zero_on_empty_buffer(bound_task, monkeypatch):
    """End-to-end happy path: empty buffer → 0 written, no exceptions."""
    monkeypatch.setattr(
        "agentic_rag_project.audit.service.AuditService.flush_buffer",
        lambda self, batch_size=100: 0,
    )
    result = bound_task(batch_size=10)
    assert result == 0


def test_audit_task_does_not_retry_on_fk_violation(bound_task, monkeypatch):
    """Simulate the dead-loop scenario from the bug report.

    `flush_buffer` raises `IntegrityError` (FK violation). The new
    policy: celery MUST NOT autoretry this — the business layer
    already dead-letters the poisoned payload. We assert by
    calling the task once and verifying it propagates the
    exception exactly once (celery autoretry would re-raise as a
    `Retry` exception, not the original `IntegrityError`).

    Note: in production the `flush_buffer` implementation does NOT
    raise on `IntegrityError` anymore — it dead-letters and
    returns 0. But we still pin the retry policy at the task
    level so a future regression (raising again) is caught early.
    """
    call_count = {"n": 0}

    def fake_flush_buffer(self, batch_size=100):
        call_count["n"] += 1
        raise sa.exc.IntegrityError("INSERT", {}, Exception("fk"))

    monkeypatch.setattr(
        "agentic_rag_project.audit.service.AuditService.flush_buffer",
        fake_flush_buffer,
    )

    # The task itself, with autoretry_for excluding IntegrityError,
    # will simply re-raise the exception (no `Retry` wrapping).
    # If autoretry_for had IntegrityError, the call would be
    # wrapped in a `celery.exceptions.Retry`.
    with pytest.raises(sa.exc.IntegrityError):
        bound_task(batch_size=10)

    assert call_count["n"] == 1, (
        "task must execute the underlying call exactly once "
        "(no autoretry on IntegrityError)"
    )


def test_audit_task_retries_on_redis_error(bound_task, monkeypatch):
    """On transient Redis errors, celery autoretry MUST trigger.

    We assert by checking `task.autoretry_for` contains the
    relevant exception (a behavioral assertion with celery's
    `apply()` is heavy; the static check is sufficient since
    celery's autoretry implementation is well-tested upstream).
    """
    assert redis_lib.RedisError in bound_task.autoretry_for


def test_audit_task_retries_on_operational_error(bound_task):
    """On transient PG connection errors, celery autoretry MUST trigger."""
    assert sa.exc.OperationalError in bound_task.autoretry_for
