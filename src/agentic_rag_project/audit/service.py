"""AuditService — Redis-buffered audit writer + WORM-aware reader.

DESIGN 2.2 #11 / TASK M4.3.1.a. Two-layer WORM:
  1. PostgreSQL trigger `audit_logs_no_modify` raises
     `insufficient_privilege` on any UPDATE or DELETE attempt
     (configured by alembic 0011).
  2. This module exposes `_immutable_update` / `_immutable_delete`
     adapter methods that raise `PermissionError` for any path
     that accidentally tries to mutate an rows (defense in depth).

Hot path:
  - `record(event)` pushes a JSON-serialized event onto the
    Redis list `audit:buffer` (DESIGN §4.4 key).
  - If Redis is unreachable, fall back to a synchronous single-row
    INSERT into PG (logger.warning) so chat latency is unaffected
    but the audit trail is never silently dropped.
  - When `metrics` is supplied, also increments the
    `audit_log_total{action, status}` Prometheus counter so the
    `HighAccessDeniedRate` alert can fire on probing attacks.

Background path:
  - Celery task `flush_audit_buffer` (every 5 s, defined in
    `agentic_rag_project.audit.tasks`) calls `flush_buffer()` to
    bulk-INSERT buffered events. Flush does NOT increment the
    Prometheus counter (it drains Redis, not record()).

Reuses the project-wide `AuditLog` ORM model (`db.models.audit`) so we
do not maintain a parallel metadata registry. `sanitized_query`
is stored under `extra["sanitized_query"]` to avoid an extra DDL
migration — the field is recovered via `to_dict()` below.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models.audit import AuditLog

from .events import AuditEvent

logger = logging.getLogger(__name__)


# Default Redis key (DESIGN §4.4 explicit).
DEFAULT_BUFFER_KEY = "audit:buffer"


# Actions that represent a denied request (probe attack signal).
# Kept in sync with the Prometheus alert
# `infra/prometheus/alerts.yaml::HighAccessDeniedRate`.
_DENIED_ACTIONS = frozenset({"access_denied", "guardrail_block"})


def _row_to_dict(row: AuditLog) -> dict:
    """Serialize an `AuditLog` row → JSON-friendly dict."""
    extra = dict(row.extra or {})
    sanitized = extra.pop("sanitized_query", None)
    return {
        "id": str(row.id),
        "ts": row.ts.isoformat() if row.ts else None,
        "user_id": str(row.user_id) if row.user_id else None,
        "workspace_id": str(row.workspace_id) if row.workspace_id else None,
        "action": row.action,
        "query": row.query,
        "sanitized_query": sanitized,
        "retrieved_doc_ids": [
            item.get("doc_id")
            for item in (row.retrieved_docs or [])
            if isinstance(item, dict) and item.get("doc_id")
        ],
        "model": row.model,
        "extra": extra,
    }


def _coerce_uuid(raw: str | uuid.UUID | None) -> uuid.UUID | None:
    """Best-effort UUID parse; None when input is None."""
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


class AuditService:
    """Buffered audit writer + WORM reader. Single instance per app."""

    def __init__(
        self,
        redis_client: redis.Redis,
        session_factory: Callable[[], Session],
        buffer_key: str = DEFAULT_BUFFER_KEY,
        metrics: object | None = None,
    ) -> None:
        self._redis = redis_client
        self._session_factory = session_factory
        self._buffer_key = buffer_key
        # `metrics` is the `MetricsRegistry` dataclass — duck-typed
        # here so this module doesn't import the observability
        # package (avoids a circular import during test collection).
        # Pass None when Prometheus isn't wired (e.g. the Celery
        # flush path drains Redis, not record()).
        self._metrics = metrics

    # ------------------------------------------------------------------
    # hot path — chat-time record
    # ------------------------------------------------------------------

    def record(self, event: AuditEvent) -> None:
        """Push event to Redis buffer; sync INSERT on Redis unavailable.

        Caller MUST NOT raise — audit failure must never bubble up to
        the chat hot path. Errors are logged at WARNING so on-call
        notices persistent Redis degradation.
        """
        # First — Prometheus sees the row even if Redis is down (and
        # even if the sync-insert fallback later fails). The alert
        # `HighAccessDeniedRate` depends on this counter being
        # incremented before the storage write.
        self._increment_metric(event.action)

        payload = json.dumps(
            asdict(event), ensure_ascii=False, default=str
        )
        try:
            self._redis.lpush(self._buffer_key, payload)
        except redis.RedisError as exc:
            logger.warning(
                "audit_buffer_unavailable, sync_write_fallback",
                extra={"error": str(exc), "action": event.action},
            )
            self._sync_insert(event)

    def _increment_metric(self, action: str) -> None:
        """Bump `audit_log_total{action, status}` for the Prometheus alert.

        Status is derived from the action: `access_denied` /
        `guardrail_block` → "denied"; everything else → "success".
        Wrapped in try/except so a metric write failure cannot
        bubble up to the chat hot path (matches the `record()`
        caller-MUST-NOT-raise contract).
        """
        if self._metrics is None:
            return
        status = "denied" if action in _DENIED_ACTIONS else "success"
        try:
            self._metrics.audit_log_total.labels(
                action=action, status=status
            ).inc()
        except Exception as exc:  # noqa: BLE001 — metric write is best-effort
            logger.warning(
                "audit_metric_increment_failed",
                extra={"error": str(exc), "action": action},
            )

    def _sync_insert(self, event: AuditEvent) -> None:
        """Direct single-row INSERT to PG (used when Redis is down)."""
        try:
            extra = dict(event.extra or {})
            if event.sanitized_query is not None:
                extra["sanitized_query"] = event.sanitized_query
            with self._session_factory() as session:
                row = AuditLog(
                    user_id=_coerce_uuid(event.user_id),
                    action=event.action,
                    query=event.query,
                    retrieved_docs=[
                        {"doc_id": d} for d in (event.retrieved_doc_ids or [])
                    ],
                    model=event.model,
                    extra=extra,
                )
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — last-resort fallback
            logger.error(
                "audit_sync_insert_failed",
                extra={"error": str(exc), "action": event.action},
            )

    # ------------------------------------------------------------------
    # background path — Celery flush
    # ------------------------------------------------------------------

    def flush_buffer(self, batch_size: int = 100) -> int:
        """Pop up to `batch_size` events, bulk INSERT to PG.

        Returns the number of rows written. Empty buffer → 0.
        Designed to be called by Celery beat every 5 s.
        """
        payloads: list[str] = []
        for _ in range(batch_size):
            raw = self._redis.rpop(self._buffer_key)
            if raw is None:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            payloads.append(raw)
        if not payloads:
            return 0

        rows_data: list[dict] = []
        for raw in payloads:
            try:
                rows_data.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning(
                    "audit_flush_drop_unparseable",
                    extra={"payload_preview": raw[:120]},
                )

        mappings: list[dict] = []
        for data in rows_data:
            extra = dict(data.get("extra") or {})
            sanitized = data.get("sanitized_query")
            if sanitized is not None:
                extra["sanitized_query"] = sanitized
            mappings.append(
                {
                    "user_id": _coerce_uuid(data.get("user_id")),
                    "action": data.get("action"),
                    "query": data.get("query"),
                    "retrieved_docs": [
                        {"doc_id": d}
                        for d in (data.get("retrieved_doc_ids") or [])
                    ],
                    "model": data.get("model"),
                    "extra": extra,
                }
            )

        if not mappings:
            return 0
        try:
            with self._session_factory() as session:
                session.bulk_insert_mappings(AuditLog, mappings)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "audit_flush_failed_requeue",
                extra={"error": str(exc), "count": len(mappings)},
            )
            for raw in reversed(payloads):
                try:
                    self._redis.lpush(self._buffer_key, raw)
                except redis.RedisError:
                    logger.error(
                        "audit_flush_requeue_failed",
                        extra={"lost": len(mappings)},
                    )
                    break
            raise
        return len(mappings)

    # ------------------------------------------------------------------
    # admin query
    # ------------------------------------------------------------------

    def query(
        self,
        user_id: str | None = None,
        action: str | None = None,
        ts_from: datetime | None = None,
        ts_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Read-only query against `audit_logs`; admin-only at API layer.

        Filters compose; `None` skips that predicate. Results are sorted
        newest-first and capped at `limit` rows.
        """
        with self._session_factory() as session:
            stmt = select(AuditLog)
            uid = _coerce_uuid(user_id)
            if uid is not None:
                stmt = stmt.where(AuditLog.user_id == uid)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            if ts_from:
                stmt = stmt.where(AuditLog.ts >= ts_from)
            if ts_to:
                stmt = stmt.where(AuditLog.ts <= ts_to)
            stmt = (
                stmt.order_by(AuditLog.ts.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = session.execute(stmt).scalars().all()
            return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # WORM adapter — fast-fail for any accidental mutator call
    # ------------------------------------------------------------------

    def _immutable_update(self, *args, **kwargs) -> None:
        """Adapter: raise PermissionError on accidental UPDATE attempts.

        Business code never calls this. The DB trigger is the
        authoritative WORM layer; this adapter exists to surface a
        readable Python error before the PG trigger fires (so dev-time
        bugs fail fast).
        """
        raise PermissionError("audit_logs is WORM: UPDATE not allowed")

    def _immutable_delete(self, *args, **kwargs) -> None:
        """Adapter: raise PermissionError on accidental DELETE attempts.

        Same rationale as `_immutable_update`.
        """
        raise PermissionError("audit_logs is WORM: DELETE not allowed")


__all__ = [
    "DEFAULT_BUFFER_KEY",
    "AuditService",
]