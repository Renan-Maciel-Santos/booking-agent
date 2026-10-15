"""Alembic environment configured for async SQLAlchemy."""

# Run the async migration entry point from Alembic's sync CLI.
import asyncio

# Configure logging from alembic.ini.
from logging.config import fileConfig

# Alembic's runtime context.
from alembic import context

# Sync connection type passed to the migration callback.
from sqlalchemy.engine import Connection

# Async engine type.
from sqlalchemy.ext.asyncio import AsyncEngine

# Single source of truth for the database URL.
from booking_agent.config import get_settings

# Target metadata for autogenerate (no tables registered yet).
from booking_agent.database.base import Base

# Engine factory shared with the application.
from booking_agent.database.engine import build_engine

# Alembic config object built from alembic.ini.
config = context.config

# Apply alembic.ini logging only when invoked with a config file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata compared against the database by `alembic revision --autogenerate`.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection."""
    # Configure the context with the URL only; no engine is created.
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    # Wrap the generated SQL in a transaction block.
    with context.begin_transaction():
        # Render every pending migration.
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    """Run migrations on a sync connection proxied from the async engine."""
    # Bind the context to the live connection.
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    # Run all migrations inside one transaction.
    with context.begin_transaction():
        # Apply pending migrations.
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    # Reuse the application's engine factory for identical connection settings.
    engine: AsyncEngine = build_engine(get_settings())
    try:
        # Open one connection for the whole migration run.
        async with engine.connect() as connection:
            # Alembic is synchronous; run_sync bridges into it.
            await connection.run_sync(_run_sync_migrations)
            # Persist the changes.
            await connection.commit()
    finally:
        # Release pooled connections.
        await engine.dispose()


# Choose the mode requested by the CLI (`--sql` means offline).
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
