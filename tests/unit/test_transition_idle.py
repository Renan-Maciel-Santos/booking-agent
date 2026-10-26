"""Unit tests for the IDLE stage transition handler."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import TemplateKey, render_template
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.idle import handle_idle
from tests.fakes.fake_clock import FakeClock
from tests.fakes.in_memory_reads import InMemoryReads


@pytest.fixture
def business_tz() -> ZoneInfo:
    """Return default business timezone."""
    return ZoneInfo("America/Sao_Paulo")


@pytest.fixture
def base_clock() -> FakeClock:
    """Return a fake clock initialized to a fixed timestamp."""
    return FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))


@pytest.fixture
def sample_customer() -> Customer:
    """Return a sample customer."""
    now_utc = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    return Customer(
        id=uuid.uuid4(),
        phone="+5511999998888",
        name="Carlos Silva",
        created_at=now_utc,
        updated_at=now_utc,
    )


@pytest.fixture
def sample_service() -> Service:
    """Return an active haircut service."""
    now_utc = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    return Service(
        id=uuid.uuid4(),
        name="Corte de Cabelo",
        duration_minutes=30,
        price_cents=4500,
        active=True,
        created_at=now_utc,
        updated_at=now_utc,
    )


@pytest.fixture
def sample_barber() -> Barber:
    """Return an active barber."""
    now_utc = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    return Barber(
        id=uuid.uuid4(),
        name="Bruno",
        active=True,
        created_at=now_utc,
        updated_at=now_utc,
    )


@pytest.mark.unit
async def test_idle_book_intent_transitions_to_collecting(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Booking intent in IDLE transitions immediately into COLLECTING_BOOKING_DETAILS."""
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots()
    intent = BookingIntent(intent=IntentType.BOOK)

    transition = await handle_idle(ctx, slots, "quero agendar", intent)

    assert transition.stage == ConversationStage.COLLECTING_BOOKING_DETAILS
    assert transition.slots.flow == ConversationFlow.BOOK
    assert transition.slots.awaiting == "service"


@pytest.mark.unit
async def test_idle_cancel_without_future_appointments_returns_not_found(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
) -> None:
    """Cancelling in IDLE with no future appointments replies that none were found."""
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        appointments={},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots()
    intent = BookingIntent(intent=IntentType.CANCEL)

    transition = await handle_idle(ctx, slots, "quero cancelar", intent)

    assert transition.stage == ConversationStage.IDLE
    assert transition.reply == render_template(TemplateKey.NO_APPOINTMENTS_FOUND)


@pytest.mark.unit
async def test_idle_cancel_with_single_appointment_enters_confirmation(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Cancelling with exactly one future appointment transitions to AWAITING_CONFIRMATION."""
    now_utc = base_clock.now()
    appt_id = uuid.uuid4()
    appt = Appointment(
        id=appt_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED.value,
        created_at=now_utc,
        updated_at=now_utc,
    )
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt_id: appt},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots()
    intent = BookingIntent(intent=IntentType.CANCEL)

    transition = await handle_idle(ctx, slots, "cancelar", intent)

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.flow == ConversationFlow.CANCEL
    assert transition.slots.appointment_id == appt_id
    assert transition.slots.action_id is not None


@pytest.mark.unit
async def test_idle_cancel_with_multiple_appointments_enters_choosing(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Cancelling with multiple future appointments transitions to CHOOSING_APPOINTMENT."""
    now_utc = base_clock.now()
    appt1_id = uuid.uuid4()
    appt2_id = uuid.uuid4()
    appt1 = Appointment(
        id=appt1_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED.value,
        created_at=now_utc,
        updated_at=now_utc,
    )
    appt2 = Appointment(
        id=appt2_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=2),
        ends_at=now_utc + timedelta(days=2, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED.value,
        created_at=now_utc,
        updated_at=now_utc,
    )
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt1_id: appt1, appt2_id: appt2},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots()
    intent = BookingIntent(intent=IntentType.CANCEL)

    transition = await handle_idle(ctx, slots, "cancelar", intent)

    assert transition.stage == ConversationStage.CHOOSING_APPOINTMENT
    assert transition.slots.flow == ConversationFlow.CANCEL
    assert transition.slots.candidate_appointment_ids == [appt1_id, appt2_id]


@pytest.mark.unit
async def test_idle_reschedule_with_single_appointment_and_datetime_enters_confirmation(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Rescheduling with 1 appointment and explicit datetime transitions to confirmation."""
    now_utc = base_clock.now()
    appt_id = uuid.uuid4()
    appt = Appointment(
        id=appt_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED.value,
        created_at=now_utc,
        updated_at=now_utc,
    )
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt_id: appt},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots()
    intent = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=date(2026, 11, 25),
        local_time=time(14, 0),
    )

    transition = await handle_idle(ctx, slots, "remarcar para 25/11 às 14h", intent)

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.flow == ConversationFlow.RESCHEDULE
    assert transition.slots.appointment_id == appt_id
    assert transition.slots.local_date == date(2026, 11, 25)
    assert transition.slots.local_time == time(14, 0)
