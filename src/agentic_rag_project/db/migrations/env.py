"""Alembic environment script.

Loads Postgres connection from app settings, points at SQLAlchemy `Base.metadata`
for autogenerate support, and runs migrations online.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from agentic_rag_project.config import get_settings
from agentic_rag_project.db.models import Base  # noqa: F401  (registers all models)
from agentic_rag_project.db.session import _build_dsn

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _override_dsn() -> None:
    """Inject the runtime DSN so alembic CLI uses the same source of truth."""
    settings = get_settings()
    config.set_main_option("sqlalchemy.url", _build_dsn(settings))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (for `alembic upgrade --sql`)."""
    _override_dsn()
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to a live database connection."""
    _override_dsn()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()