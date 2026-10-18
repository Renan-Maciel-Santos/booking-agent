"""Integration tests for database CHECK constraints."""

import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository


def _random_phone() -> str:
    """Generate a valid numeric E.164 phone number."""
    return f"+1555{secrets.randbelow(9000000) + 1000000}"


async def test_service_duration_positive_check(session: AsyncSession) -> None:
    """Verify database rejects service with duration <= 0."""
    repo = ServiceRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(
            name=f"Invalid Service {uuid.uuid4().hex[:6]}",
            duration_minutes=0,
            price_cents=1000,
        )


async def test_service_price_non_negative_check(session: AsyncSession) -> None:
    """Verify database rejects service with price < 0."""
    repo = ServiceRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(
            name=f"Invalid Price {uuid.uuid4().hex[:6]}",
            duration_minutes=30,
            price_cents=-10,
        )


async def test_appointment_ends_after_starts_check(session: AsyncSession) -> None:
    """Verify database rejects appointment where ends_at <= starts_at."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer = await cust_repo.create(name="Check Cust", phone=_random_phone())
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=2000,
    )
    barber = await barber_repo.create(name=f"Barber {uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 5, 11, 30, tzinfo=UTC)

    with pytest.raises(IntegrityError):
        await appt_repo.create(
            customer_id=customer.id,
            service_id=service.id,
            barber_id=barber.id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=service.price_cents,
        )
