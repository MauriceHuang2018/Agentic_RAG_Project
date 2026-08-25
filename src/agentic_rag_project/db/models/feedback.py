"""Feedback, FeedbackTag, FeedbackCategory models.

`feedback_tags` and `feedback_categories` are admin-configurable (CRUD via
web-admin). `feedbacks.reason_tag_id` and `feedbacks.category_id` are FKs
to these tables. `auto_categorized` flags records filled by the LLM
classifier vs. manual admin override.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentic_rag_project.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from agentic_rag_project.db.models.conversations import Message
    from agentic_rag_project.db.models.users import User


class FeedbackTag(Base, TimestampMixin):
    """Reason tag for negative feedback (e.g. wrong / inaccurate / expired)."""

    __tablename__ = "feedback_tags"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FeedbackCategory(Base, TimestampMixin):
    """Higher-level category (retrieval / chunking / generation / kb / user).

    `auto_classify_prompt` is the optional prompt fragment used by the LLM
    auto-categorizer; admins can edit it without code changes.
    """

    __tablename__ = "feedback_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    auto_classify_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Feedback(Base, TimestampMixin):
    """User 👍/👎 on an assistant message, plus optional reason + comment."""

    __tablename__ = "feedbacks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    score: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # 1=down, 5=up (per DESIGN)
    reason_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_tags.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    auto_categorized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")

    message: Mapped["Message"] = relationship("Message", back_populates="feedback")
    user: Mapped["User"] = relationship("User")
    reason_tag: Mapped["FeedbackTag | None"] = relationship("FeedbackTag")
    category: Mapped["FeedbackCategory | None"] = relationship("FeedbackCategory")