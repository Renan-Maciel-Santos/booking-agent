"""Integration test for Alembic migration upgrade and downgrade cycle."""

import asyncio

from alembic import command
from alembic.config import Config


async def test_migration_upgrade_downgrade_cycle(alembic_config: Config) -> None:
    """Verify that downgrade base and upgrade head work cleanly on a database."""
    # Downgrade to clean base
    await asyncio.to_thread(command.downgrade, alembic_config, "base")

    # Upgrade to head
    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    # Downgrade again to ensure clean drop
    await asyncio.to_thread(command.downgrade, alembic_config, "base")

    # Final upgrade to head so subsequent tests have all tables
    await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_models_and_migrations_are_in_sync(alembic_config: Config) -> None:
    """Verify that SQLAlchemy models and Alembic migrations do not diverge."""
    # Ensure database is upgraded to head before running check
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    await asyncio.to_thread(command.check, alembic_config)
