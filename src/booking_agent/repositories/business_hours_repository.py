"""Repository for business hours schedule persistence."""

from collections.abc import Sequence
from datetime import time

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import BusinessHours


class BusinessHoursRepository:
    """Repository managing BusinessHours operating schedules."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_day(self, day_of_week: int) -> BusinessHours | None:
        """Fetch operating hours for a specific weekday (0=Monday..6=Sunday).

        Args:
            day_of_week: ISO weekday integer (0 to 6).

        Returns:
            BusinessHours instance or None if closed.
        """
        stmt = select(BusinessHours).where(BusinessHours.day_of_week == day_of_week)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[BusinessHours]:
        """Fetch all configured business hours ordered by day_of_week."""
        stmt = select(BusinessHours).order_by(BusinessHours.day_of_week)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def upsert(
        self,
        day_of_week: int,
        opens_at: time,
        closes_at: time,
    ) -> BusinessHours:
        """Create or update operating hours for a weekday and flush.

        Args:
            day_of_week: ISO weekday integer (0 to 6).
            opens_at: Opening time.
            closes_at: Closing time.

        Returns:
            The persisted BusinessHours instance.
        """
        stmt = (
            insert(BusinessHours)
            .values(day_of_week=day_of_week, opens_at=opens_at, closes_at=closes_at)
            .on_conflict_do_update(
                index_elements=[BusinessHours.day_of_week],
                set_={
                    "opens_at": opens_at,
                    "closes_at": closes_at,
                    "updated_at": func.now(),
                },
            )
            .returning(BusinessHours)
            .execution_options(populate_existing=True)
        )

        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def delete(self, day_of_week: int) -> bool:
        """Remove operating hours for a weekday, marking it closed.

        Args:
            day_of_week: ISO weekday integer (0 to 6).

        Returns:
            True if a row was deleted, False otherwise.
        """
        existing = await self.get_by_day(day_of_week)
        if existing is None:
            return False
        await self._session.delete(existing)
        await self._session.flush()
        return True
