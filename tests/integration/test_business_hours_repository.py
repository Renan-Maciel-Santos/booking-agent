"""Integration tests for BusinessHoursRepository and seeds."""

from datetime import time
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.repositories.business_hours_repository import BusinessHoursRepository
from scripts.demo_conversation import CLOSED_WEEKDAYS


async def test_business_hours_crud(session: AsyncSession) -> None:
    """Verify upsert, get, list, and delete on BusinessHoursRepository."""
    repo = BusinessHoursRepository(session)

    # Day without row returns None (closed)
    assert await repo.get_by_day(0) is None

    # Upsert Monday hours (0)
    created = await repo.upsert(day_of_week=0, opens_at=time(9, 0), closes_at=time(18, 0))
    assert created.day_of_week == 0
    assert created.opens_at == time(9, 0)
    assert created.closes_at == time(18, 0)

    # Fetch by day
    fetched = await repo.get_by_day(0)
    assert fetched is not None
    assert fetched.opens_at == time(9, 0)

    # Upsert to modify hours
    updated = await repo.upsert(day_of_week=0, opens_at=time(10, 0), closes_at=time(17, 0))
    assert updated.opens_at == time(10, 0)
    assert updated.closes_at == time(17, 0)

    # List all
    all_hours = await repo.list_all()
    assert any(h.day_of_week == 0 for h in all_hours)

    # Delete marks day closed (row removed)
    deleted = await repo.delete(0)
    assert deleted is True
    assert await repo.get_by_day(0) is None


async def test_business_hours_invalid_day_range_rejected(session: AsyncSession) -> None:
    """Verify check constraint rejects day_of_week outside 0..6."""
    repo = BusinessHoursRepository(session)
    with pytest.raises(IntegrityError):
        await repo.upsert(day_of_week=7, opens_at=time(9, 0), closes_at=time(18, 0))


async def test_business_hours_closes_before_opens_rejected(session: AsyncSession) -> None:
    """Verify check constraint rejects closes_at <= opens_at."""
    repo = BusinessHoursRepository(session)
    with pytest.raises(IntegrityError):
        await repo.upsert(day_of_week=1, opens_at=time(18, 0), closes_at=time(9, 0))


async def test_demo_seed_business_hours_match_the_demo_script(session: AsyncSession) -> None:
    """Verify demo.sql defines business hours consistent with the demo script."""
    seed_path = Path(__file__).parents[2] / "db" / "seeds" / "demo.sql"
    sql = seed_path.read_text(encoding="utf-8")
    connection = await session.connection()
    raw_connection = await connection.get_raw_connection()
    driver_conn = raw_connection.driver_connection
    assert driver_conn is not None
    await driver_conn.execute(sql)
    await session.commit()
    repo = BusinessHoursRepository(session)

    # Monday (0) through Saturday (5) are open 09:00-19:00
    for day in range(6):
        hours = await repo.get_by_day(day)
        assert hours is not None
        assert hours.opens_at == time(9, 0)
        assert hours.closes_at == time(19, 0)

    # Sunday (6) is closed
    assert await repo.get_by_day(6) is None

    # The set of closed weekdays exactly matches CLOSED_WEEKDAYS from the demo script
    all_hours = await repo.list_all()
    open_weekdays = {h.day_of_week for h in all_hours}
    closed_weekdays = set(range(7)) - open_weekdays
    assert closed_weekdays == set(CLOSED_WEEKDAYS)
