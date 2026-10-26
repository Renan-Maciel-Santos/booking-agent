"""Unit tests for the COLLECTING_BOOKING_DETAILS stage transition handler."""

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from booking_agent.database.models import Barber, Customer, Service
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationFlow, ConversationStage
from booking_agent.services.booking_service import localize_datetime
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.transitions.collecting import handle_collecting_booking_details
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
async def test_collecting_missing_service_prompts_for_service(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """When service is missing, collecting stage prompts for service selection."""
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
    slots = ConversationSlots(flow=ConversationFlow.BOOK)

    transition = await handle_collecting_booking_details(ctx, slots, "olá", None)

    assert transition.stage == ConversationStage.COLLECTING_BOOKING_DETAILS
    assert transition.slots.awaiting == "service"
    assert "Corte de Cabelo" in transition.reply


@pytest.mark.unit
async def test_collecting_missing_datetime_prompts_for_datetime(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """When service is present but datetime is missing, prompts for date and time."""
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
    )

    transition = await handle_collecting_booking_details(ctx, slots, "corte", None)

    assert transition.stage == ConversationStage.COLLECTING_BOOKING_DETAILS
    assert transition.slots.awaiting == "date_time"


@pytest.mark.unit
async def test_collecting_missing_name_prompts_for_name(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """When service and datetime are present but name is missing, prompts for name."""
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
        local_date=date(2026, 11, 25),
        local_time=time(14, 0),
    )

    transition = await handle_collecting_booking_details(ctx, slots, "amanhã às 14h", None)

    assert transition.stage == ConversationStage.COLLECTING_BOOKING_DETAILS
    assert transition.slots.awaiting == "customer_name"


@pytest.mark.unit
async def test_collecting_all_slots_available_advances_to_confirmation(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """When all slots are collected and slot is available, advances to confirmation."""
    target_date = date(2026, 11, 25)
    target_time = time(14, 0)
    slot_dt = localize_datetime(target_date, target_time, business_tz)

    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        availability_slots=[slot_dt],
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
        local_date=target_date,
        local_time=target_time,
        customer_name="Carlos Silva",
    )

    transition = await handle_collecting_booking_details(
        ctx,
        slots,
        "Carlos Silva",
        BookingIntent(intent=IntentType.BOOK, customer_name="Carlos Silva"),
    )

    assert transition.stage == ConversationStage.AWAITING_CONFIRMATION
    assert transition.slots.flow == ConversationFlow.BOOK
    assert transition.slots.action_id is not None


@pytest.mark.unit
async def test_collecting_slot_unavailable_offers_alternatives(
    business_tz: ZoneInfo,
    base_clock: FakeClock,
    sample_customer: Customer,
    sample_service: Service,
    sample_barber: Barber,
) -> None:
    """When requested slot is unavailable, offers alternative slots."""
    target_date = date(2026, 11, 25)
    target_time = time(14, 0)
    alt_slot_dt = localize_datetime(target_date, time(15, 0), business_tz)

    reads = InMemoryReads(
        customers={sample_customer.id: sample_customer},
        services={sample_service.id: sample_service},
        barbers={sample_barber.id: sample_barber},
        availability_slots=[alt_slot_dt],
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
        local_date=target_date,
        local_time=target_time,
        customer_name="Carlos Silva",
    )

    transition = await handle_collecting_booking_details(
        ctx,
        slots,
        "Carlos Silva",
        BookingIntent(intent=IntentType.BOOK, customer_name="Carlos Silva"),
    )

    assert transition.stage == ConversationStage.OFFERING_ALTERNATIVES
    assert transition.slots.alternative_slots == [alt_slot_dt]
