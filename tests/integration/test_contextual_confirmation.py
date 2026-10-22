"""Integration tests for contextual confirmation scenarios with real PostgreSQL."""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.config import Settings
from booking_agent.database.models import Appointment, ConversationState
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.enums import AppointmentStatus, ConversationStage
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.business_hours_repository import BusinessHoursRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.service_repository import ServiceRepository
from booking_agent.services.booking_service import BookingService, localize_datetime
from booking_agent.services.conversation_service import ConversationService
from booking_agent.services.entity_resolver import EntityResolver
from tests.fakes.fake_clock import FakeClock


async def _setup_schedule_and_entities(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create operating business hours, a service, and an active barber."""
    hours_repo = BusinessHoursRepository(session)
    for day in range(7):
        await hours_repo.upsert(day_of_week=day, opens_at=time(8, 0), closes_at=time(20, 0))

    svc_repo = ServiceRepository(session)
    svc = await svc_repo.create(
        name=f"Corte_{uuid.uuid4().hex[:6]}",
        duration_minutes=30,
        price_cents=4500,
    )

    barber_repo = BarberRepository(session)
    barber = await barber_repo.create(name=f"Barbeiro_{uuid.uuid4().hex[:6]}")

    await session.commit()
    return svc.id, barber.id


@pytest.mark.integration
async def test_changing_time_during_reschedule_confirmation_moves_same_appointment_in_db(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Changing time during reschedule confirmation updates the same appointment in PostgreSQL."""
    now_utc = datetime.now(UTC)
    clock = FakeClock(now_utc)
    tz = ZoneInfo(integration_settings.business_timezone)
    tomorrow_date = (now_utc.astimezone(tz) + timedelta(days=1)).date()

    phone = f"+55119{uuid.uuid4().int % 100000000:08d}"

    async with session_factory() as session:
        service_id, barber_id = await _setup_schedule_and_entities(session)
        customer_repo = CustomerRepository(session)
        customer = await customer_repo.create(name="Cliente Reschedule Int", phone=phone)

        # Existing confirmed appointment at 10:00
        start_10 = localize_datetime(tomorrow_date, time(10, 0), tz)
        appt_repo = AppointmentRepository(session)
        original_appt = await appt_repo.create(
            customer_id=customer.id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=start_10,
            ends_at=start_10 + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await session.commit()
        original_id = original_appt.id

    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock, business_timezone=tz)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Turn 1: Client asks to reschedule to 14:00
    intent_parser._responses["quero remarcar meu corte para amanhã às 14h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(14, 0),
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_int_resched_1",
        phone=phone,
        text="quero remarcar meu corte para amanhã às 14h",
    )
    assert resp1 is not None
    assert "Podemos confirmar a remarcação" in resp1
    assert "14:00" in resp1

    # Turn 2: Client changes time to 17:00
    intent_parser._responses["melhor às 17h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_time=time(17, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_int_resched_2",
        phone=phone,
        text="melhor às 17h",
    )
    assert resp2 is not None
    assert "Podemos confirmar a remarcação" in resp2
    assert "17:00" in resp2

    # Turn 3: Client confirms with "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_int_resched_3",
        phone=phone,
        text="sim",
    )
    assert resp3 is not None
    assert "remarcado com sucesso" in resp3
    assert "17:00" in resp3

    # Database verification
    async with session_factory() as session:
        appts = (
            await session.scalars(select(Appointment).where(Appointment.customer_id == customer.id))
        ).all()
        assert len(appts) == 1
        final_appt = appts[0]
        assert final_appt.id == original_id
        assert final_appt.status == AppointmentStatus.CONFIRMED.value
        expected_start = localize_datetime(tomorrow_date, time(17, 0), tz)
        assert final_appt.starts_at == expected_start


@pytest.mark.integration
async def test_cancel_with_new_datetime_during_confirmation_reschedules_in_db(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """Cancelling but giving new datetime during confirmation becomes a reschedule in PostgreSQL."""
    now_utc = datetime.now(UTC)
    clock = FakeClock(now_utc)
    tz = ZoneInfo(integration_settings.business_timezone)
    tomorrow_date = (now_utc.astimezone(tz) + timedelta(days=1)).date()

    phone = f"+55119{uuid.uuid4().int % 100000000:08d}"

    async with session_factory() as session:
        service_id, barber_id = await _setup_schedule_and_entities(session)
        customer_repo = CustomerRepository(session)
        customer = await customer_repo.create(name="Cliente Cancel Int", phone=phone)

        # Existing confirmed appointment at 10:00
        start_10 = localize_datetime(tomorrow_date, time(10, 0), tz)
        appt_repo = AppointmentRepository(session)
        original_appt = await appt_repo.create(
            customer_id=customer.id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=start_10,
            ends_at=start_10 + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await session.commit()
        original_id = original_appt.id

    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock, business_timezone=tz)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Turn 1: Client asks to cancel
    intent_parser._responses["quero cancelar meu horário"] = BookingIntent(
        intent=IntentType.CANCEL,
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_int_cancel_1",
        phone=phone,
        text="quero cancelar meu horário",
    )
    assert resp1 is not None
    assert "Deseja realmente cancelar" in resp1

    # Turn 2: Client changes mind and asks for a new datetime
    intent_parser._responses["na verdade prefiro amanhã às 16h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(16, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_int_cancel_2",
        phone=phone,
        text="na verdade prefiro amanhã às 16h",
    )
    assert resp2 is not None
    assert "Podemos confirmar a remarcação" in resp2
    assert "16:00" in resp2

    # Turn 3: Client confirms with "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_int_cancel_3",
        phone=phone,
        text="sim",
    )
    assert resp3 is not None
    assert "remarcado com sucesso" in resp3
    assert "16:00" in resp3

    # Database verification
    async with session_factory() as session:
        appts = (
            await session.scalars(select(Appointment).where(Appointment.customer_id == customer.id))
        ).all()
        assert len(appts) == 1
        final_appt = appts[0]
        assert final_appt.id == original_id
        assert final_appt.status == AppointmentStatus.CONFIRMED.value
        expected_start = localize_datetime(tomorrow_date, time(16, 0), tz)
        assert final_appt.starts_at == expected_start


@pytest.mark.integration
async def test_reschedule_with_original_appointment_cancelled_mid_confirmation_fails_safe(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
) -> None:
    """When original appointment is cancelled in DB before time change, fail safe to IDLE."""
    now_utc = datetime.now(UTC)
    clock = FakeClock(now_utc)
    tz = ZoneInfo(integration_settings.business_timezone)
    tomorrow_date = (now_utc.astimezone(tz) + timedelta(days=1)).date()

    phone = f"+55119{uuid.uuid4().int % 100000000:08d}"

    async with session_factory() as session:
        service_id, barber_id = await _setup_schedule_and_entities(session)
        customer_repo = CustomerRepository(session)
        customer = await customer_repo.create(name="Cliente Cancelled Mid", phone=phone)

        # Existing confirmed appointment at 10:00
        start_10 = localize_datetime(tomorrow_date, time(10, 0), tz)
        appt_repo = AppointmentRepository(session)
        original_appt = await appt_repo.create(
            customer_id=customer.id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=start_10,
            ends_at=start_10 + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await session.commit()
        original_id = original_appt.id

    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock, business_timezone=tz)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # Turn 1: Client enters reschedule confirmation for 14:00
    intent_parser._responses["quero remarcar para amanhã às 14h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(14, 0),
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_int_mid_1",
        phone=phone,
        text="quero remarcar para amanhã às 14h",
    )
    assert resp1 is not None
    assert "Podemos confirmar a remarcação" in resp1

    # External event: the appointment is cancelled in the database directly
    async with session_factory() as session:
        appt_to_cancel = await session.get(Appointment, original_id)
        assert appt_to_cancel is not None
        appt_to_cancel.status = AppointmentStatus.CANCELLED.value
        await session.commit()

    # Turn 2: Client changes time to 16:00
    intent_parser._responses["melhor às 16h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_time=time(16, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_int_mid_2",
        phone=phone,
        text="melhor às 16h",
    )
    assert resp2 is not None
    assert "Não consegui localizar esse agendamento" in resp2

    # Database verification
    async with session_factory() as session:
        appts = (
            await session.scalars(select(Appointment).where(Appointment.customer_id == customer.id))
        ).all()
        # Exactly 1 appointment in DB, still cancelled, no duplicate created
        assert len(appts) == 1
        assert appts[0].id == original_id
        assert appts[0].status == AppointmentStatus.CANCELLED.value

        # Conversation state is reset to IDLE with empty slots
        st = await session.get(ConversationState, phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}
