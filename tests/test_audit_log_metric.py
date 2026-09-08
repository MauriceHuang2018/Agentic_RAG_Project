"""Tests for the `audit_log_total` Prometheus counter + the
`HighAccessDeniedRate` alert rule (M5 close-out, 2026-08-27).

The metric is incremented inside `AuditService.record()` once per
audit row. `status` is derived from the action: `access_denied` /
`guardrail_block` → "denied"; everything else → "success". This
file verifies:

  * the counter is declared on `MetricsRegistry` with the right
    label set
  * `record()` increments the counter (via a real `_FakeMetrics`
    stub that mimics the registry's `audit_log_total.labels(...).inc()`
    surface)
  * `access_denied` / `guardrail_block` produce `status="denied"`;
    every other action produces `status="success"`
  * metric increment is best-effort (a broken `_FakeMetrics`
    does not break `record()`)
  * Celery flush path does NOT touch the counter (it drains
    Redis, not `record()`)

The companion alert rule is asserted indirectly through the
existing `test_all_watched_metrics_have_at_least_one_alert` in
`test_alert_rules.py`, which adds `audit_log_total` to the
`WATCHED_METRICS` set.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from agentic_rag_project.audit.events import AuditAction, AuditEvent
from agentic_rag_project.audit.service import AuditService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LabeledMetric:
    """Fake `Counter.labels(action=, status=).inc()` surface."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def labels(self, **kwargs) -> "_LabeledMetric":
        self.last_labels = kwargs
        return self

    def inc(self, amount: float = 1.0) -> None:
        self.calls.append({"labels": dict(self.last_labels), "amount": amount})


class _FakeMetrics:
    """Duck-typed stand-in for `MetricsRegistry` (the only field
    the audit service reads is `audit_log_total`).

    `broken` simulates a misconfigured registry where
    `labels(...).inc()` raises — the audit service must NOT
    propagate the failure to the chat hot path.
    """

    def __init__(self, broken: bool = False) -> None:
        self.audit_log_total = _LabeledMetric("audit_log_total")
        if broken:
            self.audit_log_total.labels = MagicMock(
                side_effect=RuntimeError("metric registry down")
            )


def _redis_stub() -> MagicMock:
    """Stand-in for the Redis client; `lpush` no-ops successfully.
    No `spec=redis.Redis` because the redis package isn't on the
    dev test PYTHONPATH (it's a runtime-only dep)."""
    r = MagicMock()
    r.lpush.return_value = 1
    return r


def _session_factory_stub() -> MagicMock:
    """Stand-in for `session_factory()`; not exercised on the happy
    Redis path because we never reach `_sync_insert`."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests — metric is declared on the registry
# ---------------------------------------------------------------------------


def test_registry_declares_audit_log_total_with_action_status_labels() -> None:
    """The registry must expose `audit_log_total` with both labels."""
    from prometheus_client import CollectorRegistry

    from agentic_rag_project.observability.registry import build_registry

    reg = build_registry(CollectorRegistry())
    counter = reg.audit_log_total
    # Prometheus Counter exposes labels via `_labelnames`.
    labelnames = frozenset(counter._labelnames)
    assert labelnames == frozenset({"action", "status"}), (
        f"audit_log_total labelnames={labelnames!r}, "
        "expected exactly {action, status}"
    )


# ---------------------------------------------------------------------------
# Tests — record() bumps the counter for the right action/status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected_status",
    [
        ("access_denied", "denied"),
        ("guardrail_block", "denied"),
        ("query", "success"),
        ("feedback_submit", "success"),
        ("csat_read", "success"),
        ("role_bind", "success"),
        ("sensitive_word_update", "success"),
    ],
)
def test_record_increments_counter_with_derived_status(
    action: AuditAction, expected_status: str
) -> None:
    """`record()` must call `inc()` once with the expected status label."""
    metrics = _FakeMetrics()
    service = AuditService(
        redis_client=_redis_stub(),
        session_factory=_session_factory_stub(),
        metrics=metrics,
    )

    service.record(
        AuditEvent(
            user_id=str(uuid.uuid4()),
            action=action,
        )
    )

    labeled = metrics.audit_log_total
    assert len(labeled.calls) == 1, (
        f"expected exactly 1 inc() call, got {len(labeled.calls)}: {labeled.calls}"
    )
    assert labeled.calls[0]["labels"] == {
        "action": action,
        "status": expected_status,
    }
    assert labeled.calls[0]["amount"] == 1.0


def test_record_works_when_metrics_is_none() -> None:
    """Back-compat: callers that haven't wired metrics (e.g. legacy
    tests, the Celery flush path) must still work."""
    service = AuditService(
        redis_client=_redis_stub(),
        session_factory=_session_factory_stub(),
        # metrics=None is the default
    )
    # Should NOT raise.
    service.record(
        AuditEvent(user_id=str(uuid.uuid4()), action="feedback_submit")
    )


def test_record_swallows_metric_failures() -> None:
    """A broken Prometheus registry must NOT break the chat hot path.

    The audit-service contract is: `record()` MUST NOT raise — a
    failed metric write is logged at WARNING but swallowed.
    """
    service = AuditService(
        redis_client=_redis_stub(),
        session_factory=_session_factory_stub(),
        metrics=_FakeMetrics(broken=True),
    )
    # Should NOT raise despite the misbehaving metric stub.
    service.record(
        AuditEvent(user_id=str(uuid.uuid4()), action="access_denied")
    )


# ---------------------------------------------------------------------------
# Tests — flush_buffer() does NOT touch the counter
# ---------------------------------------------------------------------------


def test_flush_buffer_does_not_increment_metric() -> None:
    """`flush_buffer()` drains the Redis buffer (background path).
    It does NOT call `record()`, so the Prometheus counter must
    stay at zero. Otherwise every Celery flush would double-count
    rows that `record()` already incremented when they were
    originally emitted."""
    metrics = _FakeMetrics()
    service = AuditService(
        redis_client=_redis_stub(),
        session_factory=_session_factory_stub(),
        metrics=metrics,
    )
    # Empty buffer → flush_buffer returns 0 immediately.
    service._redis.rpop.return_value = None
    assert service.flush_buffer(batch_size=10) == 0
    assert metrics.audit_log_total.calls == [], (
        "flush_buffer must not touch audit_log_total; got "
        f"{metrics.audit_log_total.calls}"
    )