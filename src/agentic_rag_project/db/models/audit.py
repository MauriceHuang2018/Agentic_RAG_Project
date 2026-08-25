"""AuditLog, EvaluationResult, DriftAlert models.

`audit_logs` is WORM at the DB level — application code only INSERTs;
UPDATE/DELETE blocked by triggers (configured out-of-band in T5.6).
Partitioned monthly on `ts` (per DESIGN key indexes).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from agentic_rag_project.db.models.base import JSONB_TYPE as JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentic_rag_project.db.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    """Append-only audit record. WORM enforced at DB layer."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # 'query'|'ingest'|'delete'|'access_denied'|'guardrail_block'|...
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_docs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )  # [{doc_id, score, acl_verified}, ...]
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )


class EvaluationResult(Base):
    """One row per (message, metric). Powers Grafana dashboard aggregations."""

    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # 'recall'|'precision'|'faithfulness'|'relevance'|'citation_accuracy'|'hallucination'|'satisfaction'
    value: Mapped[float] = mapped_column(Float, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DriftAlert(Base):
    """One row per detected metric drift event (T4.4 drift_detector).

    The original T5.6 schema had only `category` + `baseline` + `current`;
    T4.4 extends it with the per-kind / per-metric / per-severity
    breakdown needed by the drift detector. The legacy `category` column
    is kept for back-compat — newer writes use `kind` + `metric_name`.
    `workspace_id IS NULL` means a global aggregate row (one per
    `(kind, metric_name)` per tick).
    """

    __tablename__ = "drift_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # New in T4.4 — replaces the original `category` semantic.
    kind: Mapped[str] = mapped_column(
        String(64), nullable=False, default="eval_quality"
    )
    metric_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown"
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="workspace"
    )
    # Legacy column — kept so existing (zero) rows survive the migration.
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline: Mapped[float] = mapped_column(Float, nullable=False)
    current: Mapped[float] = mapped_column(Float, nullable=False)
    drift_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info"
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)