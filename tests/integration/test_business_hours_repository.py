"""Integration tests for BusinessHoursRepository and seeds."""

from datetime import time

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.seeds import seed_demo_business_hours
from booking_agent.repositories.business_hours_repository import BusinessHoursRepository


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


async def test_seed_demo_business_hours(session: AsyncSession) -> None:
    """Verify demo seed populates Tuesday through Saturday."""
    await seed_demo_business_hours(session)
    repo = BusinessHoursRepository(session)

    # Monday (0) and Sunday (6) are closed (None)
    assert await repo.get_by_day(0) is None
    assert await repo.get_by_day(6) is None

    # Tuesday (1) through Saturday (5) exist
    for day in range(1, 6):
        hours = await repo.get_by_day(day)
        assert hours is not None
        assert hours.opens_at == time(9, 0)
        assert hours.closes_at == time(19, 0)
