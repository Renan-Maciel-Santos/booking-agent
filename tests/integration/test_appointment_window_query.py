"""Integration tests for AppointmentRepository time-window overlap query and rescheduling."""

import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.errors import SlotUnavailableError
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository


def _random_phone() -> str:
    """Generate a valid numeric E.164 phone number."""
    return f"+1555{secrets.randbelow(9000000) + 1000000}"


async def test_list_by_barber_and_window_overlap_semantics(session: AsyncSession) -> None:
    """Verify list_by_barber_and_window uses range overlap with half-open intervals."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer = await cust_repo.create(name="Overlap Customer", phone=_random_phone())
    barber_a = await barber_repo.create(name=f"Barber A {uuid.uuid4().hex[:6]}")
    barber_b = await barber_repo.create(name=f"Barber B {uuid.uuid4().hex[:6]}")
    service = await serv_repo.create(
        name=f"Service {uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=2500,
    )
    await session.commit()

    # Base day: 2026-11-10
    # Window: 10:00 to 12:00 UTC
    window_start = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    window_end = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    # 1. Appointment inside window: 10:30 - 11:00
    appt_inside = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_a.id,
        starts_at=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 11, 0, tzinfo=UTC),
        price_cents=2500,
    )

    # 2. Appointment partially overlapping window end: 11:45 - 12:15 (overlaps [11:45, 12:00))
    appt_overlap_end = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_a.id,
        starts_at=datetime(2026, 11, 10, 11, 45, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 12, 15, tzinfo=UTC),
        price_cents=2500,
    )

    # 3. Appointment adjacent before window (ends exactly at 10:00): 09:30 - 10:00 -> NO overlap
    await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_a.id,
        starts_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        price_cents=2500,
    )

    # 4. Appointment adjacent after window: 12:15 - 12:45 -> NO overlap
    await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_a.id,
        starts_at=datetime(2026, 11, 10, 12, 15, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 12, 45, tzinfo=UTC),
        price_cents=2500,
    )

    # 5. Cancelled appointment inside window: 11:00 - 11:30 -> should NOT be returned
    cancelled_appt = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_a.id,
        starts_at=datetime(2026, 11, 10, 11, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 11, 30, tzinfo=UTC),
        price_cents=2500,
        status=AppointmentStatus.CANCELLED,
    )

    # 6. Appointment for barber B inside window: 10:30 - 11:00 -> not returned for barber A
    await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber_b.id,
        starts_at=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
        ends_at=datetime(2026, 11, 10, 11, 0, tzinfo=UTC),
        price_cents=2500,
    )
    await session.commit()

    # Query barber A window
    results = await appt_repo.list_by_barber_and_window(barber_a.id, window_start, window_end)
    result_ids = {a.id for a in results}

    assert len(results) == 2
    assert appt_overlap_end.id in result_ids
    assert appt_inside.id in result_ids
    assert cancelled_appt.id not in result_ids


async def test_reschedule_in_place_self_overlap_and_conflict(session: AsyncSession) -> None:
    """Verify reschedule allows self-overlap but prevents collision with other bookings."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer = await cust_repo.create(name="Reschedule Customer", phone=_random_phone())
    barber = await barber_repo.create(name=f"Barber Resched {uuid.uuid4().hex[:6]}")
    service = await serv_repo.create(
        name=f"Cut {uuid.uuid4().hex[:6]}",
        duration_minutes=45,
        price_cents=3000,
    )
    await session.commit()

    # Appt 1: 10:00 - 10:45
    appt1 = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=datetime(2026, 11, 11, 10, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 11, 10, 45, tzinfo=UTC),
        price_cents=3000,
    )

    # Appt 2: 11:00 - 11:45
    await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=datetime(2026, 11, 11, 11, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 11, 11, 45, tzinfo=UTC),
        price_cents=3000,
    )
    await session.commit()

    # Self-overlap: shift appt1 by 15 min to 10:15 - 11:00 (overlaps [10:15, 10:45) with old slot)
    new_start_self = datetime(2026, 11, 11, 10, 15, tzinfo=UTC)
    new_end_self = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)
    rescheduled = await appt_repo.reschedule(appt1.id, new_start_self, new_end_self)
    assert rescheduled is not None
    assert rescheduled.starts_at == new_start_self
    assert rescheduled.ends_at == new_end_self
    await session.commit()

    # Conflict with appt 2: try shifting to 10:30 - 11:15 (overlaps [11:00, 11:15) with appt 2)
    new_start_conflict = datetime(2026, 11, 11, 10, 30, tzinfo=UTC)
    new_end_conflict = datetime(2026, 11, 11, 11, 15, tzinfo=UTC)
    with pytest.raises(SlotUnavailableError):
        await appt_repo.reschedule(appt1.id, new_start_conflict, new_end_conflict)
