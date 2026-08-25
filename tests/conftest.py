"""Test configuration & shared fixtures.

Sets up sys.path so `agentic_rag_project` is importable from the repo root
when tests are executed via `uv run pytest tests/`.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

# Ensure `src/` is on sys.path so `import agentic_rag_project` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def _is_pg_test_container_enabled() -> bool:
    """Return True unless the runner explicitly opts out.

    The fixture spins up an ephemeral Postgres via testcontainers; it
    requires a working Docker daemon. CI without Docker can skip by
    setting ``SKIP_PG_TESTCONTAINER=1``.
    """
    import os

    return os.environ.get("SKIP_PG_TESTCONTAINER", "0") != "1"


# ----- PG test container (test_indexer_upsert_idempotent) ---------------------
# ON CONFLICT upsert semantics are Postgres-specific; SQLite would mask bugs.
# The container is session-scoped (one per `pytest` invocation) so the 5-10s
# Postgres boot cost is paid once, not per test. Each test gets an isolated
# schema via the function-scoped `pg_schema_session` fixture below.

_PG_CONTAINER = None


def _start_pg_container():
    """Spin up postgres:16 in Docker; cache the container for the session."""
    global _PG_CONTAINER
    if _PG_CONTAINER is not None:
        return _PG_CONTAINER
    from testcontainers.postgres import PostgresContainer

    _PG_CONTAINER = PostgresContainer("postgres:16-alpine")
    _PG_CONTAINER.start()
    return _PG_CONTAINER


def _build_pg_dsn(container) -> str:
    """Translate testcontainers' ``get_connection_url`` into a SQLAlchemy psycopg3 URL.

    ``get_connection_url`` returns a ``postgresql+pg8000://`` URL by default;
    we want ``postgresql+psycopg://`` because that's the driver pinned in
    ``pyproject.toml``. ``host``/``port`` come from ``container.get_container_host_ip()``
    + ``container.get_exposed_port(5432)`` so the URL works from the host.
    """
    from urllib.parse import quote_plus

    user = container.username
    password = container.password
    dbname = container.dbname
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(dbname)}"
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip the indexer idempotency test when Docker is unavailable.

    Tests tagged with ``@pytest.mark.requires_pg_container`` are skipped
    unless ``SKIP_PG_TESTCONTAINER=0`` and Docker is reachable.
    """
    import pytest

    for item in items:
        if "requires_pg_container" in item.keywords and not _is_pg_test_container_enabled():
            item.add_marker(pytest.mark.skip(reason="SKIP_PG_TESTCONTAINER=1"))


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def pg_test_engine():
    """Yield a SQLAlchemy Engine pointed at an ephemeral Postgres test container.

    Boots once per ``pytest`` invocation. Skipped if ``SKIP_PG_TESTCONTAINER=1``.
    """
    if not _is_pg_test_container_enabled():
        pytest.skip("SKIP_PG_TESTCONTAINER=1")
    container = _start_pg_container()
    from sqlalchemy import create_engine

    engine = create_engine(_build_pg_dsn(container), future=True)
    yield engine
    engine.dispose()
    container.stop()


@pytest.fixture
def pg_schema_session(pg_test_engine) -> Iterator[str]:
    """Yield a fresh schema with the full ORM schema materialized.

    Creates an isolated ``test_<hex>`` schema and runs
    ``Base.metadata.create_all`` *inside* that schema (search_path), so
    every test starts from a known-empty namespace and the test's
    ``SET search_path`` keeps its writes inside the namespace only.
    """
    from sqlalchemy import text

    from agentic_rag_project.db import models as _db_models  # noqa: F401  (registers tables)
    from agentic_rag_project.db.models.base import Base

    schema = f"test_{uuid.uuid4().hex[:8]}"
    with pg_test_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        # Route DDL into the new schema. SQLAlchemy's create_all uses
        # the connection's search_path; setting it once here makes every
        # CREATE TABLE land in our isolated namespace.
        conn.execute(text(f'SET search_path TO "{schema}"'))
        Base.metadata.create_all(conn)
    yield schema
    with pg_test_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))