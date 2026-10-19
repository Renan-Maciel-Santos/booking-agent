"""Database seed utilities for local development and demonstration."""

from collections.abc import Sequence
from datetime import time

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import BusinessHours

# Default demo operating hours: Tuesday through Saturday 09:00 - 19:00.
# Monday (0) and Sunday (6) have no rows (closed).
DEMO_BUSINESS_HOURS: Sequence[tuple[int, time, time]] = [
    (1, time(9, 0), time(19, 0)),  # Tuesday
    (2, time(9, 0), time(19, 0)),  # Wednesday
    (3, time(9, 0), time(19, 0)),  # Thursday
    (4, time(9, 0), time(19, 0)),  # Friday
    (5, time(9, 0), time(19, 0)),  # Saturday
]


async def seed_demo_business_hours(session: AsyncSession) -> None:
    """Seed standard barbershop demo business hours into the database.

    Args:
        session: Active SQLAlchemy async session.
    """
    for day_of_week, opens_at, closes_at in DEMO_BUSINESS_HOURS:
        stmt = (
            insert(BusinessHours)
            .values(day_of_week=day_of_week, opens_at=opens_at, closes_at=closes_at)
            .on_conflict_do_update(
                index_elements=[BusinessHours.day_of_week],
                set_={"opens_at": opens_at, "closes_at": closes_at},
            )
        )
        await session.execute(stmt)
    await session.flush()
