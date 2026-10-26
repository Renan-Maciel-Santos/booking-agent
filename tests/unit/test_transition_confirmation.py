"""Unit tests for standalone confirmation helper functions."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.transitions.confirmation import (
    ConfirmationDetails,
    enter_confirmation,
    extract_pending_flow,
    handle_missing_confirmation_data,
    load_reschedule_origin,
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
def test_enter_confirmation_callable_standalone() -> None:
    """enter_confirmation is callable directly without a service class instance."""
    slots = ConversationSlots(flow=ConversationFlow.BOOK)
    details = ConfirmationDetails(
        service="Corte de Cabelo",
        date="25/11",
        time="14:00",
        name="Carlos Silva",
        price="R$ 45,00",
    )

    transition = enter_confirmation(ConversationFlow.BOOK, slots, details)

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.action_id is not None
    assert transition.slots.flow == ConversationFlow.BOOK
    assert "Corte de Cabelo" in transition.reply


@pytest.mark.unit
async def test_load_reschedule_origin_callable_standalone_with_in_memory_reads(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """load_reschedule_origin resolves origin appointment with InMemoryReads standalone."""
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
    slots = ConversationSlots(appointment_id=appt_id)

    loaded = await load_reschedule_origin(ctx, slots)

    assert loaded is not None
    assert loaded.id == appt_id
    assert slots.service_name == sample_service.name
    assert slots.service_price_cents == sample_service.price_cents
    assert slots.barber_name == sample_barber.name
    assert slots.customer_name == sample_customer.name
    assert slots.old_date_time is not None


@pytest.mark.unit
def test_extract_pending_flow_callable_standalone() -> None:
    """extract_pending_flow extracts flow or returns None when absent."""
    slots_with_flow = ConversationSlots(flow=ConversationFlow.CANCEL)
    assert (
        extract_pending_flow("awaiting_confirmation", slots_with_flow, "+5511999998888")
        == ConversationFlow.CANCEL
    )

    slots_without_flow = ConversationSlots()
    assert (
        extract_pending_flow("awaiting_confirmation", slots_without_flow, "+5511999998888") is None
    )


@pytest.mark.unit
def test_handle_missing_confirmation_data_callable_standalone() -> None:
    """handle_missing_confirmation_data resets state to IDLE with neutral reply."""
    slots = ConversationSlots(flow=ConversationFlow.BOOK)
    err = MissingConfirmationDataError(("service", "date"))

    transition = handle_missing_confirmation_data("+5511999998888", slots, err)

    assert transition.stage == ConversationStage.IDLE
    assert transition.slots.flow is None
