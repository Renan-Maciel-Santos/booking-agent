"""Repository that checks database connectivity."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PING_STATEMENT = text("SELECT 1")


class HealthRepository:
    """SQLAlchemy implementation of the DatabaseHealthChecker port."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Store the injected engine.

        Args:
            engine: Shared async engine created at application startup.
        """
        self._engine = engine

    async def ping(self) -> None:
        """Run a trivial query to prove a connection can be acquired and used.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database rejects the query.
            OSError: If the database host cannot be reached.
        """
        async with self._engine.connect() as connection:
            await connection.execute(_PING_STATEMENT)
