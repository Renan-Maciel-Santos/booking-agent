"""Repository that checks database connectivity."""

# Protocol lets services depend on a contract instead of a concrete class.
from typing import Protocol

# text() wraps a static SQL statement; no user input is ever interpolated here.
from sqlalchemy import text

# Engine is injected; the repository never creates its own connection pool.
from sqlalchemy.ext.asyncio import AsyncEngine

# Constant statement kept at module level to avoid rebuilding it per call.
_PING_STATEMENT = text("SELECT 1")


class DatabaseHealthChecker(Protocol):
    """Contract for anything able to verify database connectivity."""

    async def ping(self) -> None:
        """Raise if the database cannot be reached."""
        ...


class HealthRepository:
    """SQLAlchemy implementation of DatabaseHealthChecker."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Store the injected engine.

        Args:
            engine: Shared async engine created at application startup.
        """
        # Keep a reference to the shared engine.
        self._engine = engine

    async def ping(self) -> None:
        """Run a trivial query to prove a connection can be acquired and used.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database rejects the query.
            OSError: If the database host cannot be reached.
        """
        # Borrow a pooled connection for the duration of the check.
        async with self._engine.connect() as connection:
            # Execute the probe; the result itself is irrelevant.
            await connection.execute(_PING_STATEMENT)
