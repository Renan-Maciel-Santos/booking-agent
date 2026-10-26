"""Unit tests for the CHOOSING_APPOINTMENT stage transition handler."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import (
    TemplateKey,
    format_short_date_pt_br,
    format_time_pt_br,
    render_flow_cancelled,
    render_template,
)
from booking_agent.services.transitions.choosing import handle_choosing_appointment
from booking_agent.services.transitions.context import TransitionContext
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


@pytest.fixture
def sample_appointments(
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
    business_tz: ZoneInfo,
) -> tuple[Appointment, Appointment]:
    """Return two confirmed future appointments."""
    dt1 = datetime(2026, 11, 21, 14, 0, tzinfo=business_tz)
    dt2 = datetime(2026, 11, 22, 16, 0, tzinfo=business_tz)
    now_utc = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    appt1 = Appointment(
        id=uuid.uuid4(),
        customer_id=sample_customer.id,
        barber_id=sample_barber.id,
        service_id=sample_service.id,
        status=AppointmentStatus.CONFIRMED.value,
        starts_at=dt1,
        ends_at=dt1.replace(minute=30),
        created_at=now_utc,
        updated_at=now_utc,
    )
    appt2 = Appointment(
        id=uuid.uuid4(),
        customer_id=sample_customer.id,
        barber_id=sample_barber.id,
        service_id=sample_service.id,
        status=AppointmentStatus.CONFIRMED.value,
        starts_at=dt2,
        ends_at=dt2.replace(minute=30),
        created_at=now_utc,
        updated_at=now_utc,
    )
    return appt1, appt2


@pytest.mark.unit
async def test_choosing_appointment_missing_flow_resets_to_idle(
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Missing pending flow in slots triggers fail-safe reset to IDLE."""
    reads = InMemoryReads()
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(flow=None)

    transition = await handle_choosing_appointment(ctx, slots, text="1", intent=None)

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots.flow is None
    assert transition.reply == render_template(TemplateKey.GREETING_HELP)


@pytest.mark.unit
async def test_choosing_appointment_negative_response_cancels_flow(
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Negative response desists and returns flow cancelled template."""
    reads = InMemoryReads()
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(
        flow=ConversationFlow.CANCEL,
        candidate_appointment_ids=[uuid.uuid4(), uuid.uuid4()],
    )

    transition = await handle_choosing_appointment(ctx, slots, text="não", intent=None)

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots.flow is None
    assert transition.reply == render_flow_cancelled(ConversationFlow.CANCEL)


@pytest.mark.unit
async def test_choosing_appointment_valid_selection_cancel_flow(
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
    sample_appointments: tuple[Appointment, Appointment],
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Valid selection under CANCEL flow moves to AWAITING_CONFIRMATION for cancellation."""
    appt1, appt2 = sample_appointments
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt1.id: appt1, appt2.id: appt2},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots(
        flow=ConversationFlow.CANCEL,
        candidate_appointment_ids=[appt1.id, appt2.id],
    )

    transition = await handle_choosing_appointment(ctx, slots, text="1", intent=None)

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.appointment_id == appt1.id
    assert transition.slots.candidate_appointment_ids is None
    local_dt = appt1.starts_at.astimezone(business_tz)
    expected_reply = render_template(
        TemplateKey.CONFIRM_CANCEL,
        service=sample_service.name,
        date=format_short_date_pt_br(local_dt.date()),
        time=format_time_pt_br(local_dt.time()),
        barber=sample_barber.name,
    )
    assert transition.reply == expected_reply


@pytest.mark.unit
async def test_choosing_appointment_valid_selection_reschedule_without_datetime(
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
    sample_appointments: tuple[Appointment, Appointment],
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Valid selection under RESCHEDULE without new date/time prompts for new date and time."""
    appt1, appt2 = sample_appointments
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt1.id: appt1, appt2.id: appt2},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots(
        flow=ConversationFlow.RESCHEDULE,
        candidate_appointment_ids=[appt1.id, appt2.id],
    )

    transition = await handle_choosing_appointment(ctx, slots, text="segunda opção", intent=None)

    assert transition.stage == ConversationStage.COLLECTING_BOOKING_DETAILS
    assert transition.slots.appointment_id == appt2.id
    assert transition.slots.candidate_appointment_ids is None
    assert transition.reply == render_template(TemplateKey.ASK_RESCHEDULE_DATETIME)


@pytest.mark.unit
async def test_choosing_appointment_invalid_selection_reprompts_list(
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
    sample_appointments: tuple[Appointment, Appointment],
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Invalid selection out of bounds re-prompts the list of candidate appointments."""
    appt1, appt2 = sample_appointments
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={appt1.id: appt1, appt2.id: appt2},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots(
        flow=ConversationFlow.CANCEL,
        candidate_appointment_ids=[appt1.id, appt2.id],
    )

    transition = await handle_choosing_appointment(ctx, slots, text="opção 5", intent=None)

    assert transition.stage == ConversationStage.CHOOSING_APPOINTMENT
    assert transition.slots.candidate_appointment_ids == [appt1.id, appt2.id]
    assert "1. Corte de Cabelo" in transition.reply
    assert "2. Corte de Cabelo" in transition.reply
