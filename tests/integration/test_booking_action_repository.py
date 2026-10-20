"""Integration tests for BookingActionRepository and action idempotency."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.domain.errors import ActionAlreadyExistsError
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.booking_action_repository import BookingActionRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository


async def _create_test_appointment(session: AsyncSession) -> uuid.UUID:
    """Helper creating customer, barber, service and appointment in real database."""
    cust_repo = CustomerRepository(session)
    serv_repo = ServiceRepository(session)
    barber_repo = BarberRepository(session)
    appt_repo = AppointmentRepository(session)

    customer = await cust_repo.create(
        name="Action Test User", phone=f"+55119{uuid.uuid4().int % 100000000:08d}"
    )
    service = await serv_repo.create(
        name=f"Service_{uuid.uuid4().hex[:6]}", duration_minutes=30, price_cents=3500
    )
    barber = await barber_repo.create(name=f"Barber_{uuid.uuid4().hex[:6]}")
    await session.commit()

    starts_at = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    ends_at = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
    appt = await appt_repo.create(
        customer_id=customer.id,
        service_id=service.id,
        barber_id=barber.id,
        starts_at=starts_at,
        ends_at=ends_at,
        price_cents=service.price_cents,
    )
    await session.commit()
    return appt.id


async def test_record_and_get_booking_action(session: AsyncSession) -> None:
    """Verify recording a booking action and fetching it by action_id."""
    appt_id = await _create_test_appointment(session)
    action_repo = BookingActionRepository(session)
    action_id = uuid.uuid4()
    result_data = {"appointment_id": str(appt_id), "status": "confirmed"}

    action = await action_repo.record_action(
        action_id=action_id,
        kind="book",
        appointment_id=appt_id,
        result=result_data,
    )
    await session.commit()

    assert action.action_id == action_id
    assert action.kind == "book"
    assert action.appointment_id == appt_id
    assert action.result == result_data

    fetched = await action_repo.get_by_id(action_id)
    assert fetched is not None
    assert fetched.action_id == action_id
    assert fetched.result == result_data


async def test_duplicate_action_id_raises_action_already_exists_error(
    session: AsyncSession,
) -> None:
    """Verify SQLSTATE 23505 on booking_actions PK is converted to ActionAlreadyExistsError."""
    appt_id = await _create_test_appointment(session)
    action_repo = BookingActionRepository(session)
    action_id = uuid.uuid4()

    await action_repo.record_action(
        action_id=action_id,
        kind="book",
        appointment_id=appt_id,
        result={"attempt": 1},
    )
    await session.commit()

    with pytest.raises(ActionAlreadyExistsError) as exc_info:
        await action_repo.record_action(
            action_id=action_id,
            kind="book",
            appointment_id=appt_id,
            result={"attempt": 2},
        )
    assert str(action_id) in str(exc_info.value)


async def test_invalid_kind_check_constraint_propagates_integrity_error(
    session: AsyncSession,
) -> None:
    """Verify check constraint violations are not converted to ActionAlreadyExistsError."""
    appt_id = await _create_test_appointment(session)
    action_repo = BookingActionRepository(session)

    with pytest.raises(IntegrityError):
        await action_repo.record_action(
            action_id=uuid.uuid4(),
            kind="invalid_kind",
            appointment_id=appt_id,
            result={},
        )
