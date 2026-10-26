"""Unit tests for the ConversationReads protocol and UnitOfWorkReads adapter."""

import inspect
import uuid
from datetime import UTC, date, datetime
from typing import NoReturn
from unittest.mock import AsyncMock

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.models import AvailabilityResult
from booking_agent.services.booking_service import BookingService
from booking_agent.services.transitions.reads import (
    ConversationReads,
    UnitOfWorkReads,
)
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_uow import FakeUnitOfWork, InMemoryDataStore


@pytest.mark.unit
def test_conversation_reads_protocol_exposes_only_read_methods() -> None:
    """ConversationReads exposes exactly the 9 approved async query methods."""
    expected_methods = {
        "get_appointment",
        "list_customer_appointments",
        "get_customer",
        "get_customer_by_phone",
        "get_service",
        "list_active_services",
        "get_barber",
        "list_active_barbers",
        "list_availability",
    }

    public_members = [
        name
        for name, member in inspect.getmembers(ConversationReads)
        if not name.startswith("_") and inspect.isfunction(member)
    ]

    assert set(public_members) == expected_methods
    for name in public_members:
        assert name.startswith("get_") or name.startswith("list_"), (
            f"Method '{name}' does not follow read-only naming convention"
        )
        assert inspect.iscoroutinefunction(getattr(ConversationReads, name)), (
            f"Method '{name}' must be an async coroutine function"
        )


@pytest.mark.unit
async def test_unit_of_work_reads_delegates_to_repositories_and_booking_service() -> None:
    """UnitOfWorkReads delegates each read query to repositories or BookingService."""
    store = InMemoryDataStore()
    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))

    # 1. Populate test data in fake store
    customer_id = uuid.uuid4()
    customer = Customer(
        id=customer_id,
        phone="+5511999998888",
        name="Renan Santos",
    )
    store.customers[customer_id] = customer

    service_active_id = uuid.uuid4()
    service_active = Service(
        id=service_active_id,
        name="Corte Tradicional",
        duration_minutes=30,
        price_cents=4500,
        active=True,
    )
    service_inactive_id = uuid.uuid4()
    service_inactive = Service(
        id=service_inactive_id,
        name="Serviço Inativo",
        duration_minutes=30,
        price_cents=2000,
        active=False,
    )
    store.services[service_active_id] = service_active
    store.services[service_inactive_id] = service_inactive

    barber_active_id = uuid.uuid4()
    barber_active = Barber(
        id=barber_active_id,
        name="Carlos Barbeiro",
        active=True,
    )
    barber_inactive_id = uuid.uuid4()
    barber_inactive = Barber(
        id=barber_inactive_id,
        name="Barbeiro Inativo",
        active=False,
    )
    store.barbers[barber_active_id] = barber_active
    store.barbers[barber_inactive_id] = barber_inactive

    appointment_id = uuid.uuid4()
    appointment = Appointment(
        id=appointment_id,
        customer_id=customer_id,
        barber_id=barber_active_id,
        service_id=service_active_id,
        starts_at=datetime(2026, 11, 25, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 25, 14, 30, tzinfo=UTC),
        status=AppointmentStatus.CONFIRMED.value,
    )
    store.appointments[appointment_id] = appointment
    uow = FakeUnitOfWork(store, clock=clock)

    # 2. Mock BookingService for availability
    booking_service = AsyncMock(spec=BookingService)
    expected_availability = AvailabilityResult(
        service_id=service_active_id,
        target_date=date(2026, 11, 25),
        slots=[datetime(2026, 11, 25, 14, 0, tzinfo=UTC)],
        barber_id=barber_active_id,
    )
    booking_service.list_availability.return_value = expected_availability

    reads = UnitOfWorkReads(uow, booking_service)

    # 3. Assert delegation of all 9 methods
    fetched_appt = await reads.get_appointment(appointment_id)
    assert fetched_appt is not None
    assert fetched_appt.id == appointment.id
    assert await reads.get_appointment(uuid.uuid4()) is None

    customer_appts = await reads.list_customer_appointments(customer_id)
    assert len(customer_appts) == 1
    assert customer_appts[0].id == appointment.id

    cust = await reads.get_customer(customer_id)
    assert cust is not None
    assert cust.id == customer.id

    cust_phone = await reads.get_customer_by_phone("+5511999998888")
    assert cust_phone is not None
    assert cust_phone.id == customer.id
    assert await reads.get_customer_by_phone("+5511000000000") is None

    svc = await reads.get_service(service_active_id)
    assert svc is not None
    assert svc.id == service_active.id
    active_services = await reads.list_active_services()
    assert [s.id for s in active_services] == [service_active_id]

    brb = await reads.get_barber(barber_active_id)
    assert brb is not None
    assert brb.id == barber_active.id
    active_barbers = await reads.list_active_barbers()
    assert [b.id for b in active_barbers] == [barber_active_id]

    target_d = date(2026, 11, 25)
    avail_res = await reads.list_availability(
        service_id=service_active_id,
        target_date=target_d,
        barber_id=barber_active_id,
    )
    assert avail_res == expected_availability
    booking_service.list_availability.assert_awaited_once_with(
        service_id=service_active_id,
        target_date=target_d,
        barber_id=barber_active_id,
    )


@pytest.mark.unit
async def test_unit_of_work_reads_does_not_create_or_open_new_unit_of_work() -> None:
    """UnitOfWorkReads never invokes a uow_factory or creates another transaction."""
    store = InMemoryDataStore()
    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    uow = FakeUnitOfWork(store, clock=clock)

    def failing_uow_factory() -> NoReturn:
        raise AssertionError("UnitOfWorkReads must never invoke a uow_factory!")

    dummy_service_id = uuid.uuid4()
    booking_service = AsyncMock(spec=BookingService)
    booking_service.list_availability.return_value = AvailabilityResult(
        service_id=dummy_service_id,
        target_date=date(2026, 11, 25),
        slots=[],
    )

    # UnitOfWorkReads only receives the already-open uow, not a factory
    reads = UnitOfWorkReads(uow, booking_service)

    # Calling all read operations operates strictly on the existing uow without invoking any factory
    assert await reads.get_appointment(uuid.uuid4()) is None
    assert await reads.list_customer_appointments(uuid.uuid4()) == []
    assert await reads.get_customer(uuid.uuid4()) is None
    assert await reads.get_customer_by_phone("+5511999998888") is None
    assert await reads.get_service(uuid.uuid4()) is None
    assert await reads.list_active_services() == []
    assert await reads.get_barber(uuid.uuid4()) is None
    assert await reads.list_active_barbers() == []
    assert (
        await reads.list_availability(
            service_id=uuid.uuid4(),
            target_date=date(2026, 11, 25),
        )
        is not None
    )
