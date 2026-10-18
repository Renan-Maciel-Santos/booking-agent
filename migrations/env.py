"""Alembic environment configured for async SQLAlchemy."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from booking_agent.config import get_settings
from booking_agent.database import models  # noqa: F401
from booking_agent.database.base import Base
from booking_agent.database.engine import build_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata compared against the database by `alembic revision --autogenerate`.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    """Run migrations on a sync connection proxied from the async engine."""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    # Reuse the application's engine factory for identical connection settings.
    engine: AsyncEngine = build_engine(get_settings())
    try:
        async with engine.connect() as connection:
            # Alembic is synchronous; run_sync bridges into it.
            await connection.run_sync(_run_sync_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
