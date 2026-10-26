"""Unit tests for the OFFERING_ALTERNATIVES stage transition handler."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.conversation_state import (
    ANY_BARBER,
    ConversationSlots,
)
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import (
    TemplateKey,
    format_currency_brl,
    format_short_date_pt_br,
    format_time_pt_br,
    render_flow_cancelled,
    render_template,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.offering import handle_offering_alternatives
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
async def test_offering_alternatives_choose_by_number(
    sample_service: Service,
    sample_barber: Barber,
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Choosing an alternative slot by number moves to AWAITING_CONFIRMATION."""
    alt1 = datetime(2026, 11, 21, 14, 0, tzinfo=business_tz)
    alt2 = datetime(2026, 11, 21, 15, 0, tzinfo=business_tz)
    reads = InMemoryReads(
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(
        flow=ConversationFlow.BOOK,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        customer_name="Carlos Silva",
        alternative_slots=[alt1, alt2],
    )

    transition = await handle_offering_alternatives(ctx, slots, text="1", intent=None)

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.local_date == alt1.date()
    assert transition.slots.local_time == alt1.time()
    assert transition.slots.alternative_slots is None
    expected_reply = render_template(
        TemplateKey.CONFIRM_BOOKING,
        service=sample_service.name,
        date=format_short_date_pt_br(alt1.date()),
        time=format_time_pt_br(alt1.time()),
        barber=sample_barber.name,
        name="Carlos Silva",
        price=format_currency_brl(sample_service.price_cents),
    )
    assert transition.reply == expected_reply


@pytest.mark.unit
async def test_offering_alternatives_choose_by_time(
    sample_service: Service,
    sample_barber: Barber,
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Choosing an alternative slot by matching time string moves to AWAITING_CONFIRMATION."""
    alt1 = datetime(2026, 11, 21, 14, 0, tzinfo=business_tz)
    alt2 = datetime(2026, 11, 21, 15, 0, tzinfo=business_tz)
    reads = InMemoryReads(
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(
        flow=ConversationFlow.BOOK,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        barber_id=ANY_BARBER,
        barber_name=None,
        customer_name="Carlos Silva",
        alternative_slots=[alt1, alt2],
    )

    transition = await handle_offering_alternatives(
        ctx, slots, text="quero as 15h por favor", intent=None
    )

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.local_date == alt2.date()
    assert transition.slots.local_time == alt2.time()
    assert transition.slots.alternative_slots is None


@pytest.mark.unit
async def test_offering_alternatives_unrecognized_response_prompts_clarification(
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Unrecognized response prompts clarification without changing stage."""
    alt1 = datetime(2026, 11, 21, 14, 0, tzinfo=business_tz)
    reads = InMemoryReads()
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(
        flow=ConversationFlow.BOOK,
        alternative_slots=[alt1],
    )

    transition = await handle_offering_alternatives(ctx, slots, text="tanto faz", intent=None)

    assert transition.stage == ConversationStage.OFFERING_ALTERNATIVES
    assert transition.slots.alternative_slots == [alt1]
    assert transition.reply == render_template(TemplateKey.CLARIFICATION_PROMPT)


@pytest.mark.unit
async def test_offering_alternatives_negative_response_cancels_flow(
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """Negative response desists and returns flow cancelled response."""
    reads = InMemoryReads()
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )
    slots = ConversationSlots(
        flow=ConversationFlow.BOOK,
        alternative_slots=[datetime(2026, 11, 21, 14, 0, tzinfo=business_tz)],
    )

    transition = await handle_offering_alternatives(ctx, slots, text="não", intent=None)

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots.flow is None
    assert transition.reply == render_flow_cancelled(ConversationFlow.BOOK)


@pytest.mark.unit
async def test_offering_alternatives_reschedule_invalid_origin_resets_to_idle(
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
    base_clock: FakeClock,
    business_tz: ZoneInfo,
) -> None:
    """In RESCHEDULE flow, an invalid/cancelled origin appointment fails safe to IDLE."""
    now_utc = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    cancelled_appt = Appointment(
        id=uuid.uuid4(),
        customer_id=sample_customer.id,
        barber_id=sample_barber.id,
        service_id=sample_service.id,
        status=AppointmentStatus.CANCELLED.value,
        starts_at=datetime(2026, 11, 21, 14, 0, tzinfo=business_tz),
        ends_at=datetime(2026, 11, 21, 14, 30, tzinfo=business_tz),
        created_at=now_utc,
        updated_at=now_utc,
    )
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        appointments={cancelled_appt.id: cancelled_appt},
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
        appointment_id=cancelled_appt.id,
        alternative_slots=[datetime(2026, 11, 22, 16, 0, tzinfo=business_tz)],
    )

    transition = await handle_offering_alternatives(ctx, slots, text="1", intent=None)

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots.flow is None
    assert transition.reply == render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND)
