"""M5 test helper — minimal SQLAlchemy metadata for F2 workspace check.

M5 F2 (security/feedback_authorization) added a service-layer check that
queries `messages.conversation.workspace_id` to verify the target message
belongs to the claimed workspace. The legacy `tests/test_feedback.py`
engine fixture uses `feedback.models._Base` which only declares the
feedback tables, so service.submit would fail with
`no such table: messages` for every test that exercises the service.

To keep the existing tests passing without bootstrapping the full DB
(which would require re-declaring every model), this module exposes a
tiny parallel declarative that owns just the four tables needed by the
F2 lookup: `workspaces`, `conversations`, `messages`, `users`.

The schema is intentionally minimal — only the columns read by the F2
path are declared. Any column missing from the runtime query will
SQL-fail at the first SELECT; if M5 ever needs more columns, add them
here.
"""
from __future__ import annotations

import uuid

from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class _M5MinBase(DeclarativeBase):
    """Parallel declarative — owns only the tables F2 reads from."""


class User(_M5MinBase):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Workspace(_M5MinBase):
    __tablename__ = "workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Conversation(_M5MinBase):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Reverse side so `Message.conversation.workspace_id` works
    # through the relationship without a JOIN.
    messages = relationship("Message", back_populates="conversation")


class Message(_M5MinBase):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False, default="")
    # The ORM `Message` declares a `metadata_` JSONB column; SQLite
    # doesn't support JSONB, so use JSON which is JSONB-compatible
    # for the SELECT the F2 check performs.
    metadata_ = Column(
        "metadata", JSON, nullable=False, server_default="{}"
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    conversation = relationship("Conversation", back_populates="messages")


# Public name consumed by `tests/test_feedback.py::engine` and any
# future test that needs the same minimum table set.
_M5_MIN_TABLES = _M5MinBase.metadata

__all__ = ["_M5_MIN_TABLES"]