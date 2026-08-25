"""Document, Chunk, ACL models.

CHUNK has no `level` field (removed — see 6A consensus). Only `is_parent`
bool distinguishes parent (chapter-level) vs child (paragraph-level) chunks.
Vector storage lives in Qdrant; this table stores metadata only.

ACL `permission_key` is a free-form string that references
`permissions.key` semantically (no FK to keep ACL independent from RBAC
permission renaming — DESIGN 4.2). Super-admin and owner overrides still
apply regardless.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from agentic_rag_project.db.models.base import JSONB_TYPE as JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentic_rag_project.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from agentic_rag_project.db.models.users import User, Workspace


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base, TimestampMixin):
    """Uploaded document. Soft-delete via `deleted_at`."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # 'txt'|'pdf'|'word'|'excel'|'ppt'|'image'
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # 'pending'|'processing'|'ready'|'failed'
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    authority_level: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="documents")
    owner: Mapped["User"] = relationship(
        "User", back_populates="documents", foreign_keys=[owner_id]
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        "Chunk", back_populates="document", cascade="all, delete-orphan"
    )
    acls: Mapped[list["ACL"]] = relationship(
        "ACL", back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base, TimestampMixin):
    """Metadata for a parent or child chunk. Vectors live in Qdrant."""

    __tablename__ = "chunks"
    __table_args__ = (
        # Used by `chunks(content_hash)` for dedup (DESIGN key indexes)
        {"postgresql_with_oids": False},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # sha256 hex
    is_parent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # True=chapter-level parent, False=paragraph-level child
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=True,
    )
    position: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )  # {page, section_path, ...}

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")
    parent: Mapped["Chunk | None"] = relationship(
        "Chunk", remote_side=lambda: [Chunk.id], foreign_keys=[parent_chunk_id]
    )


class ACL(Base):
    """Explicit per-document access grant.

    Exactly one of `user_id` or `workspace_id` should be set; `permission_key`
    is a free-form string aligned with RBAC `permissions.key` (e.g.
    `doc:read`, `doc:download`, `doc:comment`).
    """

    __tablename__ = "acls"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) OR (workspace_id IS NOT NULL)",
            name="ck_acls_principal_required",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    permission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    document: Mapped["Document"] = relationship("Document", back_populates="acls")