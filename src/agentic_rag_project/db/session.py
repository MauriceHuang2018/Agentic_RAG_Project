"""SQLAlchemy engine + session factory.

`get_engine()` reads POSTGRES_* settings and constructs a psycopg3-backed
engine. `get_session()` returns a context-managed session for short-lived
operations (FastAPI dependencies use `get_db` below for proper per-request
scope).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from agentic_rag_project.config import Settings, get_settings


def _build_dsn(settings: Settings) -> str:
    """Compose a PostgreSQL DSN from settings, URL-escaping the password."""
    password = quote_plus(settings.postgres_password)
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine for the configured Postgres."""
    settings = get_settings()
    return create_engine(
        _build_dsn(settings),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
    )


# Public alias for short-lived sessions outside FastAPI request scope
# (startup hooks, CLI commands, background jobs). The FastAPI `get_db`
# dependency still uses `_session_factory` directly for per-request scoping.
SessionLocal = _session_factory()


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session, committing/rolling back.

    Usage:
        @router.get(...)
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    session = _session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()