"""Sensitive-value ORM model (M6 / Page 15 / T1.3b).

`SensitiveValue` backs `/admin/sensitive-info` (system_admin only).
It stores both system presets (seeded by migration 0005 from
`post_processor.filter.DEFAULT_SENSITIVE_WORDS`) and admin-added
custom words. Every write triggers `sensitive_sync()` which pushes
the active word list into Redis `sensitive:words` and rebuilds the
in-process `SensitiveWordFilter` Trie.

Categories are free-form strings to accommodate future taxonomies
without DDL changes. `is_preset=True` rows are system-provided and
cannot be deleted by the API (enforced at the FastAPI layer, not DB).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_rag_project.db.models.base import Base

if TYPE_CHECKING:
    from agentic_rag_project.db.models.audit import AuditLog
    from agentic_rag_project.db.models.users import User


class SensitiveValue(Base):
    """A single sensitive word. `is_active=False` excludes it from
    the live filter without losing the row (admin can re-enable)."""

    __tablename__ = "sensitive_values"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    word: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Free-form category string: 'political' | 'porn' | 'violence' |
    # 'scam' | 'profanity' | 'custom' | future categories.
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="custom"
    )
    is_preset: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_by: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_audit_id: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audit_logs.id", ondelete="SET NULL"),
        nullable=True,
    )