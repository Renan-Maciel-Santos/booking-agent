"""Unit tests for BookingService business rules, scheduling, and conflict recovery."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from booking_agent.config import Settings
from booking_agent.database.models import Appointment, Barber, BusinessHours, Customer, Service
from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.errors import (
    AppointmentInThePastError,
    AppointmentNotFoundError,
    InvalidLocalTimeError,
    InvalidTimeWindowError,
    MinimumNoticeViolationError,
    NotAppointmentOwnerError,
    OutsideBusinessHoursError,
    SlotInThePastError,
    SlotUnavailableError,
)
from booking_agent.services.booking_service import BookingService, localize_datetime
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_uow import FakeAppointmentRepository, FakeUnitOfWorkFactory, InMemoryDataStore

SP_TZ = ZoneInfo("America/Sao_Paulo")


@pytest.fixture
def base_settings() -> Settings:
    """Return standard test settings."""
    return build_test_settings(
        business_timezone="America/Sao_Paulo",
        slot_granularity_minutes=15,
        min_booking_advance_minutes=60,
        max_booking_horizon_days=30,
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    """Return fixed clock set to 2026-10-20 12:00:00 UTC (Tuesday 09:00:00 Sao Paulo)."""
    return FakeClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))


@pytest.fixture
def populated_store() -> tuple[InMemoryDataStore, Barber, Barber, Service, Customer]:
    """Return an in-memory store prepopulated with barbers, services, hours, and customers."""
    store = InMemoryDataStore()

    # Barbers: Alex and Bruno (ordered alphabetically)
    barber1 = Barber(id=uuid.uuid4(), name="Alex", active=True)
    barber2 = Barber(id=uuid.uuid4(), name="Bruno", active=True)
    store.barbers[barber1.id] = barber1
    store.barbers[barber2.id] = barber2

    # Service: Haircut (30 minutes, 3500 cents)
    service = Service(
        id=uuid.uuid4(),
        name="Haircut",
        duration_minutes=30,
        price_cents=3500,
        active=True,
    )
    store.services[service.id] = service

    # Customer: Bob
    customer = Customer(
        id=uuid.uuid4(),
        name="Bob",
        phone="+5511999990001",
    )
    store.customers[customer.id] = customer

    # Business hours: Tuesday (weekday 1) to Saturday (weekday 5): 09:00 to 18:00
    # Sunday (6) and Monday (0) have NO row in business_hours (closed days).
    for day in range(1, 6):
        store.business_hours[day] = BusinessHours(
            day_of_week=day,
            opens_at=time(9, 0),
            closes_at=time(18, 0),
        )

    return store, barber1, barber2, service, customer


# ---------------------------------------------------------------------------
# 1. Localize datetime and DST handling
# ---------------------------------------------------------------------------


def test_localize_datetime_sao_paulo_and_fold() -> None:
    """Verify local time conversion to timezone-aware datetime with fold=0."""
    target_d = date(2026, 10, 20)
    target_t = time(14, 30)
    dt = localize_datetime(target_d, target_t, SP_TZ)

    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 20
    assert dt.hour == 14
    assert dt.minute == 30
    assert dt.fold == 0
    assert dt.tzinfo == SP_TZ


def test_localize_datetime_dst_spring_forward_gap() -> None:
    """Verify InvalidLocalTimeError is raised for non-existent wall-clock times in DST gap."""
    ny_tz = ZoneInfo("America/New_York")
    # In New York on 2026-03-08, 02:00 springs forward to 03:00; 02:30 does not exist.
    with pytest.raises(InvalidLocalTimeError) as exc_info:
        localize_datetime(date(2026, 3, 8), time(2, 30), ny_tz)
    assert "does not exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Availability rules
# ---------------------------------------------------------------------------


async def test_list_availability_closed_day(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify list_availability returns empty slots on closed days (e.g. Sunday/Monday)."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # 2026-10-25 is a Sunday (weekday 6) - no row in business_hours
    sunday = date(2026, 10, 25)
    result = await service_obj.list_availability(service.id, sunday, barber_id=barber1.id)
    assert result.slots == []


async def test_list_availability_granularity_and_advance_notice(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify slot generation respects slot_granularity_minutes and min_booking_advance_minutes."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Clock is at 2026-10-20 12:00 UTC = 09:00 Sao Paulo.
    # min_advance = 60 min -> earliest slot must be >= 10:00 Sao Paulo.
    # Opens at 09:00, closes at 18:00, service duration = 30m.
    # Last candidate must finish by 18:00 -> latest start is 17:30.
    tuesday = date(2026, 10, 20)
    result = await service_obj.list_availability(service.id, tuesday, barber_id=barber1.id)

    assert len(result.slots) > 0
    # First available slot should be 10:00
    assert result.slots[0].time() == time(10, 0)
    # Granularity: slots separated by 15 minutes
    assert result.slots[1].time() == time(10, 15)
    assert result.slots[2].time() == time(10, 30)
    # Last slot: 17:30 (finishes at 18:00)
    assert result.slots[-1].time() == time(17, 30)


async def test_list_availability_unions_any_barber(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify list_availability with barber_id=None unions slots across active barbers."""
    store, barber1, _, service, customer = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    target_date = date(2026, 10, 21)  # Wednesday
    # Barber 1 is occupied on Wednesday 10:00 - 10:30
    slot_utc = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # 10:00 Sao Paulo
    slot_end_utc = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)
    async with FakeUnitOfWorkFactory(store)() as uow:
        await uow.appointments.create(
            customer_id=customer.id,
            service_id=service.id,
            barber_id=barber1.id,
            starts_at=slot_utc,
            ends_at=slot_end_utc,
            price_cents=3500,
        )

    # Query with barber_id=None -> 10:00 is STILL available because Barber 2 is free!
    result_union = await service_obj.list_availability(service.id, target_date, barber_id=None)
    times_union = [s.time() for s in result_union.slots]
    assert time(10, 0) in times_union

    # Query specifically for Barber 1 -> 10:00 and 10:15 are NOT available!
    result_b1 = await service_obj.list_availability(service.id, target_date, barber_id=barber1.id)
    times_b1 = [s.time() for s in result_b1.slots]
    assert time(10, 0) not in times_b1
    assert (
        time(10, 15) not in times_b1
    )  # 30min service starting at 10:15 would overlap 10:00-10:30!


async def test_list_availability_horizon_validation(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify past dates and dates beyond max horizon raise InvalidTimeWindowError."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Past date
    with pytest.raises(InvalidTimeWindowError):
        await service_obj.list_availability(service.id, date(2026, 10, 19), barber1.id)

    # Exceeds max horizon (30 days from 2026-10-20 is 2026-11-19)
    with pytest.raises(InvalidTimeWindowError):
        await service_obj.list_availability(service.id, date(2026, 11, 25), barber1.id)


# ---------------------------------------------------------------------------
# 3. Booking rules
# ---------------------------------------------------------------------------


async def test_book_past_and_minimum_notice(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify book rejects past slots and slots violating minimum notice."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Past slot
    past_dt = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    with pytest.raises(SlotInThePastError):
        await service_obj.book("+5511999990001", "Alice", service.id, past_dt, barber1.id)

    # 30 min in future (less than min_booking_advance_minutes=60)
    soon_dt = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
    with pytest.raises(MinimumNoticeViolationError):
        await service_obj.book("+5511999990001", "Alice", service.id, soon_dt, barber1.id)


async def test_book_timezone_naive_rejected(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify timezone-naive datetime raises ValueError."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    naive_dt = datetime(2026, 10, 21, 14, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        await service_obj.book("+5511999990001", "Alice", service.id, naive_dt, barber1.id)


async def test_book_outside_business_hours(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify booking on closed day or outside hours raises OutsideBusinessHoursError."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Sunday 2026-10-25 14:00 (closed)
    sunday_dt = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)  # 14:00 Sao Paulo
    with pytest.raises(OutsideBusinessHoursError, match="closed"):
        await service_obj.book("+5511999990001", "Alice", service.id, sunday_dt, barber1.id)

    # Tuesday before opening: 08:30 (opens at 09:00)
    early_dt = datetime(2026, 10, 21, 11, 30, tzinfo=UTC)  # 08:30 Sao Paulo
    with pytest.raises(OutsideBusinessHoursError, match="outside business hours"):
        await service_obj.book("+5511999990001", "Alice", service.id, early_dt, barber1.id)

    # Tuesday ending after closing: 17:45 for 30m service (closes at 18:00)
    late_dt = datetime(2026, 10, 21, 20, 45, tzinfo=UTC)  # 17:45 Sao Paulo
    with pytest.raises(OutsideBusinessHoursError, match="outside business hours"):
        await service_obj.book("+5511999990001", "Alice", service.id, late_dt, barber1.id)


async def test_book_preserves_existing_customer_name(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify that booking with an existing phone number does not overwrite their name."""
    store, barber1, _, service, existing_customer = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    target_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # Wednesday 10:00 Sao Paulo

    # Attempt to book using existing_customer's phone, but a different name (e.g. spouse/child)
    result = await service_obj.book(
        customer_phone=existing_customer.phone,
        customer_name="Alice (Spouse)",
        service_id=service.id,
        starts_at=target_dt,
        barber_id=barber1.id,
    )

    assert result.appointment.customer_id == existing_customer.id
    # The persisted customer name remains the original "Bob"
    assert store.customers[existing_customer.id].name == "Bob"


async def test_book_deterministic_any_barber(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify book without barber_id deterministically picks first available barber by name."""
    store, barber1, barber2, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Target slot: Wednesday 10:00 Sao Paulo
    target_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)

    # Both Alex and Bruno are free -> Alex is picked first alphabetically
    result1 = await service_obj.book(
        "+5511999991111", "Client 1", service.id, target_dt, barber_id=None
    )
    assert result1.appointment.barber_id == barber1.id

    # Another client books the exact same slot without barber -> Bruno is picked next!
    result2 = await service_obj.book(
        "+5511999992222", "Client 2", service.id, target_dt, barber_id=None
    )
    assert result2.appointment.barber_id == barber2.id

    # Third client tries the same slot -> both occupied -> SlotUnavailableError with alternatives!
    with pytest.raises(SlotUnavailableError) as exc_info:
        await service_obj.book("+5511999993333", "Client 3", service.id, target_dt, barber_id=None)
    assert len(exc_info.value.alternatives) > 0


async def test_book_any_barber_retries_next_barber_on_conflict(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify book with barber_id=None tries next free barber when first fails on insert."""
    store, barber1, barber2, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    target_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)

    # Simulate race-condition conflict specifically on barber1 insert
    orig_create = FakeAppointmentRepository.create

    async def fail_barber1_create(
        self: FakeAppointmentRepository,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        barber_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        price_cents: int,
        status: AppointmentStatus = AppointmentStatus.CONFIRMED,
        appointment_id: uuid.UUID | None = None,
    ) -> Appointment:
        if barber_id == barber1.id:
            raise SlotUnavailableError("Simulated concurrent race on barber 1.")
        return await orig_create(
            self,
            customer_id=customer_id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=price_cents,
            status=status,
            appointment_id=appointment_id,
        )

    monkeypatch.setattr(FakeAppointmentRepository, "create", fail_barber1_create)

    result = await service_obj.book(
        "+5511999990001", "Client", service.id, target_dt, barber_id=None
    )
    assert result.appointment.barber_id == barber2.id


async def test_book_conflict_rollback_and_alternatives(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify SlotUnavailableError triggers UoW rollback and queries alternatives in new session."""
    store, barber1, _, service, _ = populated_store
    uow_factory = FakeUnitOfWorkFactory(store)
    service_obj = BookingService(uow_factory, fake_clock, base_settings)

    target_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # 10:00 Sao Paulo

    # 1. First booking succeeds
    await service_obj.book("+5511999990001", "Client 1", service.id, target_dt, barber1.id)

    # 2. Second booking on same barber and slot fails
    initial_appt_count = len(store.appointments)
    with pytest.raises(SlotUnavailableError) as exc_info:
        await service_obj.book("+5511999990002", "Client 2", service.id, target_dt, barber1.id)

    # Rollback verification: no new appointment was added
    assert len(store.appointments) == initial_appt_count

    # Alternatives were queried in new UoW and attached to exception
    assert exc_info.value.alternatives is not None
    assert len(exc_info.value.alternatives) > 0
    # The conflicting slot (10:00 Sao Paulo) must not be in alternatives
    assert target_dt.astimezone(SP_TZ) not in exc_info.value.alternatives


# ---------------------------------------------------------------------------
# 4. Reschedule rules
# ---------------------------------------------------------------------------


async def test_reschedule_self_overlap_succeeds(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify shifting appointment by 15 min (self-overlap) succeeds without collision."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Book 10:00 - 10:30 (13:00 - 13:30 UTC)
    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    # Reschedule to 10:15 - 10:45 (13:15 - 13:45 UTC) -> overlaps [10:15, 10:30) of old slot
    new_start_dt = datetime(2026, 10, 21, 13, 15, tzinfo=UTC)
    resched = await service_obj.reschedule(res.appointment.id, "+5511999990001", new_start_dt)

    assert resched.appointment.id == res.appointment.id
    assert resched.appointment.starts_at == new_start_dt
    assert resched.appointment.ends_at == datetime(2026, 10, 21, 13, 45, tzinfo=UTC)


async def test_reschedule_conflict_with_another_booking(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify reschedule collision raises SlotUnavailableError with alternatives."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    # Appt 1: 10:00 - 10:30
    res1 = await service_obj.book(
        "+5511999990001", "Bob", service.id, datetime(2026, 10, 21, 13, 0, tzinfo=UTC), barber1.id
    )
    # Appt 2: 11:00 - 11:30
    await service_obj.book(
        "+5511999990002", "Alice", service.id, datetime(2026, 10, 21, 14, 0, tzinfo=UTC), barber1.id
    )

    # Try moving Appt 1 into 11:00 - 11:30 (collides with Appt 2)
    with pytest.raises(SlotUnavailableError) as exc_info:
        await service_obj.reschedule(
            res1.appointment.id, "+5511999990001", datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
        )
    assert len(exc_info.value.alternatives) > 0


async def test_reschedule_ownership_and_status_preconditions(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify reschedule checks ownership, confirmed status, and past restrictions."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    new_dt = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)

    # Wrong phone -> NotAppointmentOwnerError
    with pytest.raises(NotAppointmentOwnerError):
        await service_obj.reschedule(res.appointment.id, "+5511999999999", new_dt)

    # Non-existent appointment -> AppointmentNotFoundError
    with pytest.raises(AppointmentNotFoundError):
        await service_obj.reschedule(uuid.uuid4(), "+5511999990001", new_dt)

    # Advance clock past appointment start -> AppointmentInThePastError
    fake_clock.set_time(datetime(2026, 10, 21, 13, 30, tzinfo=UTC))
    with pytest.raises(AppointmentInThePastError):
        await service_obj.reschedule(
            res.appointment.id, "+5511999990001", datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
        )


# ---------------------------------------------------------------------------
# 5. Cancellation rules
# ---------------------------------------------------------------------------


async def test_cancel_idempotence(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify cancel marks status=cancelled and repeating cancel is idempotent."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    # First cancel
    c1 = await service_obj.cancel(res.appointment.id, "+5511999990001")
    assert c1.appointment.status == AppointmentStatus.CANCELLED.value

    # Second cancel (idempotent, returns cancelled appointment without error)
    c2 = await service_obj.cancel(res.appointment.id, "+5511999990001")
    assert c2.appointment.status == AppointmentStatus.CANCELLED.value


async def test_cancel_ownership_and_past_checks(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify cancel enforces customer ownership and rejects cancelling past appointments."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    # Wrong customer phone -> NotAppointmentOwnerError
    with pytest.raises(NotAppointmentOwnerError):
        await service_obj.cancel(res.appointment.id, "+5511999999999")

    # Past appointment -> AppointmentInThePastError
    fake_clock.set_time(datetime(2026, 10, 21, 14, 0, tzinfo=UTC))
    with pytest.raises(AppointmentInThePastError):
        await service_obj.cancel(res.appointment.id, "+5511999990001")


# ---------------------------------------------------------------------------
# 6. Action idempotency via booking_actions
# ---------------------------------------------------------------------------


async def test_book_with_action_id_is_idempotent(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify book with the same action_id returns the existing appointment."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)
    action_id = uuid.uuid4()

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    res1 = await service_obj.book(
        "+5511999990001",
        "Bob",
        service.id,
        start_dt,
        barber1.id,
        action_id=action_id,
    )
    assert len(store.appointments) == 1
    assert action_id in store.booking_actions

    # Re-executing with the same action_id returns original appointment without duplicating
    res2 = await service_obj.book(
        "+5511999990001",
        "Bob",
        service.id,
        start_dt,
        barber1.id,
        action_id=action_id,
    )
    assert res2.appointment.id == res1.appointment.id
    assert len(store.appointments) == 1


async def test_reschedule_with_action_id_is_idempotent(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify reschedule with the same action_id returns the cached result."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    book_res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    action_id = uuid.uuid4()
    new_dt = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    res1 = await service_obj.reschedule(
        book_res.appointment.id,
        "+5511999990001",
        new_dt,
        action_id=action_id,
    )
    assert res1.appointment.starts_at == new_dt

    # Re-executing reschedule with the same action_id
    res2 = await service_obj.reschedule(
        book_res.appointment.id,
        "+5511999990001",
        new_dt,
        action_id=action_id,
    )
    assert res2.appointment.id == res1.appointment.id
    assert res2.appointment.starts_at == new_dt


async def test_delayed_old_reschedule_does_not_overwrite_newer_reschedule(
    base_settings: Settings,
    fake_clock: FakeClock,
    populated_store: tuple[InMemoryDataStore, Barber, Barber, Service, Customer],
) -> None:
    """Verify delayed re-delivery of an old reschedule action does not overwrite a newer one."""
    store, barber1, _, service, _ = populated_store
    service_obj = BookingService(FakeUnitOfWorkFactory(store), fake_clock, base_settings)

    start_dt = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    book_res = await service_obj.book("+5511999990001", "Bob", service.id, start_dt, barber1.id)

    # Turn 1: Reschedule to 14:00 with action_id_A
    action_id_a = uuid.uuid4()
    dt_a = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    await service_obj.reschedule(
        book_res.appointment.id,
        "+5511999990001",
        dt_a,
        action_id=action_id_a,
    )
    assert store.appointments[book_res.appointment.id].starts_at == dt_a

    # Turn 2: Reschedule to 15:00 with action_id_B
    action_id_b = uuid.uuid4()
    dt_b = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    await service_obj.reschedule(
        book_res.appointment.id,
        "+5511999990001",
        dt_b,
        action_id=action_id_b,
    )
    assert store.appointments[book_res.appointment.id].starts_at == dt_b

    # Turn 3: Delayed re-delivery of action_id_a (proposing old 14:00)
    delayed_res = await service_obj.reschedule(
        book_res.appointment.id,
        "+5511999990001",
        dt_a,
        action_id=action_id_a,
    )
    # The appointment in store MUST remain at 15:00 (dt_b)!
    assert store.appointments[book_res.appointment.id].starts_at == dt_b
    assert delayed_res.appointment.id == book_res.appointment.id
