"""Factories for the async SQLAlchemy engine and session maker."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from booking_agent.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine for the configured database.

    Args:
        settings: Application settings with database parameters.

    Returns:
        A pooled AsyncEngine; the caller owns its lifecycle and must dispose it.
    """
    # pool_pre_ping discards connections dropped by the server (e.g. after a DB restart).
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to the given engine.

    Args:
        engine: Engine created by build_engine.

    Returns:
        Factory producing AsyncSession objects.
    """
    # expire_on_commit=False keeps loaded attributes usable after commit in async code,
    # where lazy reloads would otherwise fail.
    return async_sessionmaker(engine, expire_on_commit=False)
