"""Integration tests for BookingService against a real PostgreSQL database with testcontainers."""

import asyncio
import uuid
from datetime import UTC, date, datetime, time
from typing import Self

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.config import Settings
from booking_agent.database.models import Appointment, Barber
from booking_agent.database.unit_of_work import (
    SqlAlchemyUnitOfWork,
    SqlAlchemyUnitOfWorkFactory,
)
from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.errors import SlotUnavailableError
from booking_agent.domain.models import BookingResult
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.business_hours_repository import BusinessHoursRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository
from booking_agent.services.booking_service import BookingService
from tests.fakes.fake_clock import FakeClock


async def _setup_business_schedule(session: AsyncSession) -> None:
    """Set up standard Tuesday-Saturday 09:00-18:00 business hours."""
    hours_repo = BusinessHoursRepository(session)
    for day in range(1, 6):
        await hours_repo.upsert(day_of_week=day, opens_at=time(9, 0), closes_at=time(18, 0))
    await session.commit()


async def test_list_availability_excludes_occupied_slots(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Verify list_availability excludes slots overlapping confirmed bookings in real PostgreSQL."""
    async with session_factory() as session:
        await _setup_business_schedule(session)
        barber_repo = BarberRepository(session)
        service_repo = ServiceRepository(session)
        customer_repo = CustomerRepository(session)

        barber = await barber_repo.create(name=f"Barber_{uuid.uuid4().hex[:6]}")
        service = await service_repo.create(
            name=f"Haircut_{uuid.uuid4().hex[:6]}", duration_minutes=30, price_cents=3000
        )
        customer = await customer_repo.create(
            name="Jane", phone=f"+55119{uuid.uuid4().int % 100000000:08d}"
        )
        await session.commit()

    # Simulated clock: Tuesday 2026-11-17 12:00 UTC (09:00 Sao Paulo)
    clock = FakeClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)

    # Book 10:00 - 10:30 Sao Paulo (13:00 - 13:30 UTC)
    target_dt = datetime(2026, 11, 17, 13, 0, tzinfo=UTC)
    book_res = await booking_service.book(
        customer_phone=customer.phone,
        customer_name=customer.name,
        service_id=service.id,
        starts_at=target_dt,
        barber_id=barber.id,
    )
    assert book_res.appointment is not None

    # Query availability for that day
    target_date = date(2026, 11, 17)
    availability = await booking_service.list_availability(service.id, target_date, barber.id)

    times = [s.time() for s in availability.slots]
    # 10:00 (occupied) and 10:15 (overlaps [10:15, 10:45) with [10:00, 10:30)) are excluded
    assert time(10, 0) not in times
    assert time(10, 15) not in times
    # 10:30 and 10:45 must be available
    assert time(10, 30) in times
    assert time(10, 45) in times


async def test_reschedule_allows_self_overlap_without_exclusion_violation(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Verify shifting an appointment by 15 min (self-overlap) succeeds in PostgreSQL."""
    async with session_factory() as session:
        await _setup_business_schedule(session)
        barber = await BarberRepository(session).create(name=f"Barber_{uuid.uuid4().hex[:6]}")
        service = await ServiceRepository(session).create(
            name=f"Coloring_{uuid.uuid4().hex[:6]}", duration_minutes=45, price_cents=5000
        )
        phone = f"+55119{uuid.uuid4().int % 100000000:08d}"
        await session.commit()

    clock = FakeClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)

    # Initial booking: 10:00 - 10:45 Sao Paulo (13:00 - 13:45 UTC)
    start_dt = datetime(2026, 11, 17, 13, 0, tzinfo=UTC)
    book_res = await booking_service.book(
        customer_phone=phone,
        customer_name="Alice",
        service_id=service.id,
        starts_at=start_dt,
        barber_id=barber.id,
    )
    appt_id = book_res.appointment.id

    # Reschedule to 10:15 - 11:00 (overlaps [10:15, 10:45) of its own old slot)
    new_start_dt = datetime(2026, 11, 17, 13, 15, tzinfo=UTC)
    resched_res = await booking_service.reschedule(
        appointment_id=appt_id,
        customer_phone=phone,
        new_starts_at=new_start_dt,
    )

    assert resched_res.appointment.id == appt_id
    assert resched_res.appointment.starts_at == new_start_dt
    assert resched_res.appointment.ends_at == datetime(2026, 11, 17, 14, 0, tzinfo=UTC)

    # Verify state in database
    async with session_factory() as session:
        persisted = await session.get(Appointment, appt_id)
        assert persisted is not None
        assert persisted.starts_at == new_start_dt
        assert persisted.ends_at == datetime(2026, 11, 17, 14, 0, tzinfo=UTC)


async def test_concurrent_bookings_conflict_resolution_and_clean_rollback(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Verify concurrent bookings: winner commits, loser gets alternatives and clean rollback."""
    async with session_factory() as session:
        await _setup_business_schedule(session)
        barber = await BarberRepository(session).create(name=f"Barber_{uuid.uuid4().hex[:6]}")
        service = await ServiceRepository(session).create(
            name=f"Cut_{uuid.uuid4().hex[:6]}", duration_minutes=30, price_cents=3500
        )
        phone1 = f"+55119{uuid.uuid4().int % 100000000:08d}"
        phone2 = f"+55119{uuid.uuid4().int % 100000000:08d}"
        await session.commit()

    clock = FakeClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)

    # Target slot: 11:00 - 11:30 Sao Paulo (14:00 - 14:30 UTC)
    target_slot = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)

    # Launch two concurrent book operations for the same slot and barber
    results = await asyncio.gather(
        booking_service.book(phone1, "User 1", service.id, target_slot, barber.id),
        booking_service.book(phone2, "User 2", service.id, target_slot, barber.id),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, BookingResult)]
    failures = [r for r in results if isinstance(r, SlotUnavailableError)]

    assert len(successes) == 1, f"Expected 1 winner, got: {results}"
    assert len(failures) == 1, f"Expected 1 SlotUnavailableError, got: {results}"

    loser_error = failures[0]
    assert len(loser_error.alternatives) > 0
    # The contested slot must not be in the alternatives
    assert target_slot.astimezone(booking_service.business_timezone) not in loser_error.alternatives

    # Verify database consistency: exactly one appointment was persisted
    async with session_factory() as session:
        stmt = select(Appointment).where(
            Appointment.barber_id == barber.id,
            Appointment.starts_at == target_slot,
        )
        persisted = (await session.execute(stmt)).scalars().all()
        assert len(persisted) == 1
        assert persisted[0].id == successes[0].appointment.id

        # Verify session is clean and operable (no InFailedSqlTransaction leak)
        count_stmt = select(Appointment)
        all_appts = (await session.execute(count_stmt)).scalars().all()
        assert len(all_appts) >= 1


async def test_any_barber_assignment_and_availability_union(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Verify 'any barber' logic: unions free slots and deterministically selects."""
    async with session_factory() as session:
        await _setup_business_schedule(session)
        # Create two barbers with deterministic names
        b_alpha = await BarberRepository(session).create(name=f"Alpha_{uuid.uuid4().hex[:6]}")
        b_beta = await BarberRepository(session).create(name=f"Beta_{uuid.uuid4().hex[:6]}")
        # Deactivate any other barbers in the test container so only Alpha and Beta are active
        stmt = update(Barber).where(Barber.id.notin_([b_alpha.id, b_beta.id])).values(active=False)
        await session.execute(stmt)
        service = await ServiceRepository(session).create(
            name=f"Shave_{uuid.uuid4().hex[:6]}", duration_minutes=30, price_cents=2500
        )
        phone1 = f"+55119{uuid.uuid4().int % 100000000:08d}"
        phone2 = f"+55119{uuid.uuid4().int % 100000000:08d}"
        phone3 = f"+55119{uuid.uuid4().int % 100000000:08d}"
        await session.commit()

    clock = FakeClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)

    target_slot = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)  # 11:00 Sao Paulo

    # 1. First booking with barber_id=None -> selects first barber alphabetically (Alpha)
    book1 = await booking_service.book(phone1, "Client 1", service.id, target_slot, barber_id=None)
    assert book1.appointment.barber_id == b_alpha.id

    # 2. list_availability with barber_id=None -> 11:00 is STILL available because Beta is free!
    target_date = date(2026, 11, 17)
    avail1 = await booking_service.list_availability(service.id, target_date, barber_id=None)
    times1 = [s.time() for s in avail1.slots]
    assert time(11, 0) in times1

    # 3. Second booking with barber_id=None for the same slot -> selects Beta
    book2 = await booking_service.book(phone2, "Client 2", service.id, target_slot, barber_id=None)
    assert book2.appointment.barber_id == b_beta.id

    # 4. list_availability with barber_id=None -> 11:00 is now UNAVAILABLE (both occupied)
    avail2 = await booking_service.list_availability(service.id, target_date, barber_id=None)
    times2 = [s.time() for s in avail2.slots]
    assert time(11, 0) not in times2

    # 5. Third booking with barber_id=None -> raises SlotUnavailableError with alternatives
    with pytest.raises(SlotUnavailableError) as exc_info:
        await booking_service.book(phone3, "Client 3", service.id, target_slot, barber_id=None)
    assert len(exc_info.value.alternatives) > 0
    contested_local = target_slot.astimezone(booking_service.business_timezone)
    assert contested_local not in exc_info.value.alternatives


async def test_any_barber_retry_on_concurrent_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Verify any-barber booking retries next free barber in new UoW when first suffers conflict."""
    async with session_factory() as session:
        await _setup_business_schedule(session)
        # Create two barbers with names sorting first alphabetically
        b_alpha = await BarberRepository(session).create(name=f"0000_Alpha_{uuid.uuid4().hex[:6]}")
        b_beta = await BarberRepository(session).create(name=f"0001_Beta_{uuid.uuid4().hex[:6]}")
        service = await ServiceRepository(session).create(
            name=f"Trim_{uuid.uuid4().hex[:6]}", duration_minutes=30, price_cents=3000
        )
        phone = f"+55119{uuid.uuid4().int % 100000000:08d}"
        await session.commit()

    clock = FakeClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    target_slot = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)  # 11:00 Sao Paulo

    interfered_flag: list[bool] = []

    class InterceptingAppointmentRepository(AppointmentRepository):
        async def create(
            self,
            customer_id: uuid.UUID,
            service_id: uuid.UUID,
            barber_id: uuid.UUID,
            starts_at: datetime,
            ends_at: datetime,
            price_cents: int,
            status: AppointmentStatus = AppointmentStatus.CONFIRMED,
            appointment_id: uuid.UUID | None = None,
        ) -> Appointment:
            if barber_id == b_alpha.id and not interfered_flag:
                interfered_flag.append(True)
                # Concurrently reserve b_alpha via another session and commit to PostgreSQL
                async with session_factory() as concurrent_session:
                    other_cust = await CustomerRepository(concurrent_session).create(
                        name="Interloper",
                        phone=f"+55119{uuid.uuid4().int % 100000000:08d}",
                    )
                    await AppointmentRepository(concurrent_session).create(
                        customer_id=other_cust.id,
                        service_id=service_id,
                        barber_id=b_alpha.id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        price_cents=price_cents,
                        status=status,
                    )
                    await concurrent_session.commit()

            return await super().create(
                customer_id=customer_id,
                service_id=service_id,
                barber_id=barber_id,
                starts_at=starts_at,
                ends_at=ends_at,
                price_cents=price_cents,
                status=status,
                appointment_id=appointment_id,
            )

    class InterceptingUnitOfWork(SqlAlchemyUnitOfWork):
        async def __aenter__(self) -> Self:
            await super().__aenter__()
            assert self._session is not None
            self._appointments = InterceptingAppointmentRepository(self._session)
            return self

    booking_service = BookingService(
        lambda: InterceptingUnitOfWork(session_factory),
        clock,
        integration_settings,
    )

    result = await booking_service.book(
        phone, "Target Client", service.id, target_slot, barber_id=None
    )

    assert len(interfered_flag) == 1
    # Successfully retried in a new unit of work and selected the second barber
    assert result.appointment.barber_id == b_beta.id

    # Verify both appointments are persisted in PostgreSQL
    async with session_factory() as session:
        appts = (
            (
                await session.execute(
                    select(Appointment).where(
                        Appointment.starts_at == target_slot,
                        Appointment.barber_id.in_([b_alpha.id, b_beta.id]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(appts) == 2
