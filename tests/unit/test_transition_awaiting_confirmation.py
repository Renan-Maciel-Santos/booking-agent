"""Unit tests for the AWAITING_CONFIRMATION stage transition handler."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.services.booking_service import localize_datetime
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import (
    TemplateKey,
    render_flow_cancelled,
    render_template,
)
from booking_agent.services.transitions.awaiting_confirmation import (
    handle_awaiting_confirmation,
)
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


@pytest.mark.unit
async def test_unrecognized_response_prompts_clarification_without_executing(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Unrecognized response re-prompts clarification without executing or changing slots."""
    now_utc = base_clock.now()
    appt_id = uuid.uuid4()
    origin_appt = Appointment(
        id=appt_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED,
        created_at=now_utc,
        updated_at=now_utc,
    )
    reads = InMemoryReads(
        appointments={appt_id: origin_appt},
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
    slots = ConversationSlots(
        flow=ConversationFlow.CANCEL,
        appointment_id=appt_id,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        customer_name=sample_customer.name,
        local_date=date(2026, 11, 21),
        local_time=time(14, 0),
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        old_date_time="21/11 às 14:00",
        action_id=uuid.uuid4(),
    )

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="qual o endereço de vocês?",
        intent=None,
    )

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.reply == render_template(TemplateKey.CLARIFICATION_PROMPT)
    assert transition.slots.action_id == slots.action_id


@pytest.mark.unit
async def test_time_change_in_reschedule_returns_updated_reschedule_confirmation(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Time change intent in RESCHEDULE returns reschedule confirmation for the same appointment."""
    now_utc = base_clock.now()
    appt_id = uuid.uuid4()
    origin_appt = Appointment(
        id=appt_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED,
        created_at=now_utc,
        updated_at=now_utc,
    )
    new_date = date(2026, 11, 22)
    new_time = time(16, 0)
    target_dt_utc = localize_datetime(new_date, new_time, business_tz)

    reads = InMemoryReads(
        appointments={appt_id: origin_appt},
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        availability_slots=[target_dt_utc],
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    initial_action_id = uuid.uuid4()
    slots = ConversationSlots(
        flow=ConversationFlow.RESCHEDULE,
        appointment_id=appt_id,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        customer_name=sample_customer.name,
        local_date=new_date,
        local_time=time(14, 0),
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        old_date_time="21/11 às 10:00",
        action_id=initial_action_id,
    )
    intent = BookingIntent(local_time=new_time)

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="pode mudar para as 16h?",
        intent=intent,
    )

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.flow == ConversationFlow.RESCHEDULE
    assert transition.slots.appointment_id == appt_id
    assert transition.slots.local_time == new_time
    assert transition.slots.action_id != initial_action_id
    assert "16:00" in transition.reply


@pytest.mark.unit
async def test_new_date_in_cancel_converts_to_reschedule_confirmation(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """New date intent in CANCEL flow converts to reschedule confirmation."""
    now_utc = base_clock.now()
    appt_id = uuid.uuid4()
    origin_appt = Appointment(
        id=appt_id,
        customer_id=sample_customer.id,
        service_id=sample_service.id,
        barber_id=sample_barber.id,
        starts_at=now_utc + timedelta(days=1),
        ends_at=now_utc + timedelta(days=1, minutes=30),
        price_cents=4500,
        status=AppointmentStatus.CONFIRMED,
        created_at=now_utc,
        updated_at=now_utc,
    )
    new_date = date(2026, 11, 25)
    new_time = time(14, 0)
    target_dt_utc = localize_datetime(new_date, new_time, business_tz)

    reads = InMemoryReads(
        appointments={appt_id: origin_appt},
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        availability_slots=[target_dt_utc],
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
        appointment_id=appt_id,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        customer_name=sample_customer.name,
        local_date=date(2026, 11, 21),
        local_time=new_time,
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        old_date_time="21/11 às 10:00",
        action_id=uuid.uuid4(),
    )
    intent = BookingIntent(local_date=new_date)

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="melhor mudar para o dia 25",
        intent=intent,
    )

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.flow == ConversationFlow.RESCHEDULE
    assert transition.slots.appointment_id == appt_id
    assert transition.slots.local_date == new_date
    assert "25/11" in transition.reply


@pytest.mark.unit
async def test_pode_cancelar_in_book_desists(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Saying 'pode cancelar' in BOOK flow aborts booking and returns flow-cancelled template."""
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
    slots = ConversationSlots(
        flow=ConversationFlow.BOOK,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        customer_name=sample_customer.name,
        local_date=date(2026, 11, 22),
        local_time=time(14, 0),
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        action_id=uuid.uuid4(),
    )

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="pode cancelar",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots == ConversationSlots()
    assert transition.reply == render_flow_cancelled(ConversationFlow.BOOK)


@pytest.mark.unit
async def test_missing_flow_resets_to_idle_without_executing(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
) -> None:
    """Missing pending flow in slots safely resets conversation to IDLE."""
    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
    )
    ctx = TransitionContext(
        reads=reads,
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone=sample_customer.phone,
    )
    slots = ConversationSlots(flow=None)

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="sim",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots == ConversationSlots()
    assert transition.reply == render_template(TemplateKey.GREETING_HELP)


@pytest.mark.unit
async def test_invalid_origin_resets_to_idle_with_origin_not_found(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """Missing or invalid reschedule origin safely resets to IDLE with error template."""
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
    unknown_appt_id = uuid.uuid4()
    slots = ConversationSlots(
        flow=ConversationFlow.RESCHEDULE,
        appointment_id=unknown_appt_id,
        service_id=sample_service.id,
        service_name=sample_service.name,
        service_price_cents=sample_service.price_cents,
        customer_name=sample_customer.name,
        local_date=date(2026, 11, 22),
        local_time=time(14, 0),
        barber_id=sample_barber.id,
        barber_name=sample_barber.name,
        action_id=uuid.uuid4(),
    )
    intent = BookingIntent(local_time=time(16, 0))

    transition = await handle_awaiting_confirmation(
        ctx=ctx,
        slots=slots,
        text="pode ser as 16h?",
        intent=intent,
    )

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots == ConversationSlots()
    assert transition.reply == render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND)
