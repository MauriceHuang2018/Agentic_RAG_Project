"""Declarative base + common mixins for SQLAlchemy ORM models.

`Base` is imported by every model class and by Alembic env.py for
`target_metadata`. `TimestampMixin` adds created_at / updated_at; UUID
primary keys are explicit per-model so that column ordering in migrations
matches DESIGN 4.1.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


# JSONB on Postgres, JSON elsewhere. Lets unit tests use in-memory SQLite
# without sacrificing the production-grade JSONB column type. DDL emitted
# by Alembic stays JSONB (it runs against Postgres only).
JSONB_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


class TimestampMixin:
    """Adds `created_at` and (where needed) `updated_at` columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )