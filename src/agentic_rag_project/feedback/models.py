"""Feedback ORM models (T4.2).

DESIGN 4.6 / TASK T4.2 — user feedback capture, auto-attribution,
and (lightweight) ticket status tracking. Five categories and
seven ticket statuses are system-predefined; both are stored in
data tables so admins can extend without code changes.

Tables:
  * `feedbacks`             — user submission (like/dislike/comment)
  * `feedback_attributions` — auto_attributor output + reasoning
  * `feedback_categories`   — 5 system presets + admin extensions
  * `feedback_tags`         — free-form tags (admin maintained)
  * `ticket_statuses`       — 7 system presets + admin extensions
  * `feedback_tickets`      — ticket state machine + routing

Naming note: we use `attributions` (root-cause) rather than
`categorizations` so the column names match the user's pseudocode
(`feedback.attribution_category`). The whole pipeline has a single
concept: "why did this answer go wrong".

State-machine transitions are kept in code (see
`agentic_rag_project.feedback.ticket_state`) — only the set of
*legal status values* is data-driven. Reasoning: the transition
graph encodes business rules (e.g. `user_query` auto-closes), and
admin-tunable transitions would couple workflow changes to schema
edits, which is harder to reason about than a 20-line dict.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class FeedbackRating(str, enum.Enum):
    """User verdict on the assistant's answer."""

    LIKE = "like"
    DISLIKE = "dislike"


class AttributionStatus(str, enum.Enum):
    """Status of the auto-attribution pipeline for one feedback row."""

    PENDING = "pending"             # not yet run
    SUCCEEDED = "succeeded"
    FAILED = "failed"               # attributor raised — submission still kept
    SKIPPED = "skipped"             # user_query category → no further work


class _Base(DeclarativeBase):
    pass


class FeedbackCategory(_Base):
    """One row in the attribution dictionary.

    `is_system` flags the 5 built-ins so admins can't accidentally
    delete them. New rows from the admin UI default to `is_system=False`.
    """

    __tablename__ = "feedback_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FeedbackTag(_Base):
    """Free-form tag dictionary.

    Tags are lower-priority signals than categories — admins attach
    them to categorise manual triage buckets (e.g. "vpn-issue",
    "weekend-query"). The model doesn't enforce uniqueness on
    `tag_key` at the application layer because admins may want to
    alias; uniqueness is enforced via a unique index instead.
    """

    __tablename__ = "feedback_tags"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tag_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TicketStatus(_Base):
    """One row in the ticket-state dictionary.

    System seeds 7 rows at startup; admins may add more. The
    `key` field is the value `feedback_tickets.status` holds —
    a String column with FK back to this table. The legal
    *transitions* between statuses are still in code (see
    `agentic_rag_project.feedback.ticket_state`), because the
    graph encodes business policy rather than user preference.
    """

    __tablename__ = "ticket_statuses"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Feedback(_Base):
    """A user's feedback on a single assistant message."""

    __tablename__ = "feedbacks"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    rating: Mapped[FeedbackRating] = mapped_column(
        Enum(FeedbackRating, name="feedback_rating"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    ragas_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attribution_status: Mapped[AttributionStatus] = mapped_column(
        Enum(AttributionStatus, name="attribution_status"),
        nullable=False,
        default=AttributionStatus.PENDING,
        server_default=AttributionStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships — lazy-loaded so we don't JOIN on every read.
    attribution: Mapped["FeedbackAttribution | None"] = relationship(
        "FeedbackAttribution",
        back_populates="feedback",
        uselist=False,
        cascade="all, delete-orphan",
    )
    ticket: Mapped["FeedbackTicket | None"] = relationship(
        "FeedbackTicket",
        back_populates="feedback",
        uselist=False,
        cascade="all, delete-orphan",
    )


class FeedbackAttribution(_Base):
    """Output of `AutoAttributor` for one feedback.

    Stored separately from `feedbacks` so re-running the attributor
    is non-destructive (each attempt is a new row) — same pattern
    as `EvaluationResult`.
    """

    __tablename__ = "feedback_attributions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    feedback_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feedbacks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feedback_categories.id"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    feedback: Mapped[Feedback] = relationship("Feedback", back_populates="attribution")
    category: Mapped[FeedbackCategory] = relationship("FeedbackCategory")


class FeedbackTicket(_Base):
    """Lightweight ticket-state machine for one feedback.

    Created automatically when attribution succeeds. `status`
    holds the `key` from `ticket_statuses` (FK), so admins can
    extend the dictionary without a code change. The legal
    state transitions live in
    `agentic_rag_project.feedback.ticket_state` — this model
    only persists the current state.
    """

    __tablename__ = "feedback_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    feedback_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feedbacks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("ticket_statuses.key", ondelete="RESTRICT"),
        nullable=False,
        default="collected",
        server_default="collected",
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    feedback: Mapped[Feedback] = relationship("Feedback", back_populates="ticket")


# Composite index for the dashboard query: "give me all feedback for
# workspace X that hit category Y in the last N days".
Index(
    "ix_feedbacks_workspace_rating_created",
    Feedback.workspace_id,
    Feedback.rating,
    Feedback.created_at,
)


__all__ = [
    "AttributionStatus",
    "Feedback",
    "FeedbackAttribution",
    "FeedbackCategory",
    "FeedbackRating",
    "FeedbackTag",
    "FeedbackTicket",
    "TicketStatus",
]
