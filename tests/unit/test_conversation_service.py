"""Unit tests for ConversationService state machine and orchestration with fakes."""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from booking_agent.config import Settings
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConfirmationCandidate,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import LLMInvalidOutputError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.services.booking_service import BookingService, localize_datetime
from booking_agent.services.conversation_service import (
    ConversationService,
    classify_fast_path_candidate,
    is_valid_customer_name,
    mask_phone,
    resolve_confirmation,
)
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import TemplateKey, render_template
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_uow import FakeUnitOfWorkFactory, InMemoryDataStore


def _create_unit_fixtures() -> tuple[
    ConversationService,
    FakeUnitOfWorkFactory,
    FakeClock,
    FakeIntentParser,
    Settings,
    uuid.UUID,
    uuid.UUID,
]:
    """Build ConversationService with fake dependencies and in-memory storage."""
    clock = FakeClock(datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    store = InMemoryDataStore()
    uow_factory = FakeUnitOfWorkFactory(store, clock=clock)
    settings = build_test_settings(
        conversation_ttl_minutes=30,
        business_timezone="America/Sao_Paulo",
    )

    # Seed business hours for every day
    for day in range(7):
        bh_mock = type(
            "BH",
            (),
            {"day_of_week": day, "opens_at": time(8, 0), "closes_at": time(20, 0)},
        )()
        store.business_hours[day] = bh_mock

    # Seed an active service and an active barber
    svc_id = uuid.uuid4()
    store.services[svc_id] = type(
        "Svc",
        (),
        {
            "id": svc_id,
            "name": "Corte de Cabelo",
            "duration_minutes": 30,
            "price_cents": 4500,
            "active": True,
        },
    )()

    barber_id = uuid.uuid4()
    store.barbers[barber_id] = type(
        "Brb",
        (),
        {
            "id": barber_id,
            "name": "Carlos Barber",
            "active": True,
        },
    )()

    intent_parser = FakeIntentParser(clock=clock)
    entity_resolver = EntityResolver()
    booking_service = BookingService(uow_factory, clock, settings)

    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=settings,
    )
    return conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id


@pytest.mark.unit
async def test_ttl_expiration_with_was_in_progress_returns_session_expired() -> None:
    """If state was in progress (not idle or has slots) and TTL expired, return SESSION_EXPIRED."""
    conv_service, uow_factory, clock, _, _, svc_id, _ = _create_unit_fixtures()
    phone = "+5511999991111"

    # Seed state in progress updated 45 minutes ago (TTL = 30m)
    stale_time = clock.now() - timedelta(minutes=45)
    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS.value,
            slots={"service_id": str(svc_id)},
        )
    uow_factory.store.conversation_states[phone].updated_at = stale_time

    resp = await conv_service.handle_message(
        message_id="msg_ttl_1",
        phone=phone,
        text="Olá",
    )
    assert "Sua sessão anterior expirou por inatividade" in resp

    # Verify state was cleanly reset to IDLE and empty slots
    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}


@pytest.mark.unit
async def test_ttl_expiration_when_idle_does_not_show_expired_warning() -> None:
    """If state was already idle and has no slots, return standard GREETING_HELP."""
    conv_service, uow_factory, clock, _, _, _, _ = _create_unit_fixtures()
    phone = "+5511999991112"

    # Seed state in IDLE with empty slots, updated 45 minutes ago
    stale_time = clock.now() - timedelta(minutes=45)
    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.IDLE.value,
            slots={},
        )
    uow_factory.store.conversation_states[phone].updated_at = stale_time

    resp = await conv_service.handle_message(
        message_id="msg_ttl_2",
        phone=phone,
        text="Olá",
    )
    assert "Sua sessão anterior expirou" not in resp
    assert "Sou o assistente da Northside Barbers" in resp


@pytest.mark.unit
async def test_registered_customer_name_is_never_prompted() -> None:
    """If customer already exists in the database, customer_name is auto-loaded and not asked."""
    conv_service, uow_factory, _clock, intent_parser, _, _svc_id, _ = _create_unit_fixtures()
    phone = "+5511999991113"

    # Pre-register customer in customer repository
    async with uow_factory() as uow:
        await uow.customers.create(name="Henrique Cadastrado", phone=phone)

    # Configure intent with service, date, time but NO customer_name
    intent_parser._responses["quero agendar corte amanha as 15h"] = BookingIntent(
        intent=IntentType.BOOK,
        service_name="Corte de Cabelo",
        local_date=datetime(2026, 11, 21, tzinfo=UTC).date(),
        local_time=time(15, 0),
        customer_name=None,
    )

    resp = await conv_service.handle_message(
        message_id="msg_cust_reg_1",
        phone=phone,
        text="quero agendar corte amanha as 15h",
    )

    # The customer name was auto-populated from database, skipping ASK_CUSTOMER_NAME prompt!
    assert "qual é o seu nome" not in resp
    assert "Podemos confirmar?" in resp
    assert "Henrique Cadastrado" in resp


@pytest.mark.unit
async def test_fast_path_affirmative_rejected_if_state_not_awaiting_confirmation() -> None:
    """A fast-path 'sim' message is discarded if the locked state is not AWAITING_CONFIRMATION."""
    conv_service, uow_factory, _clock, _, _, _, _ = _create_unit_fixtures()
    phone = "+5511999991114"

    # State is IDLE
    resp = await conv_service.handle_message(
        message_id="msg_sim_idle",
        phone=phone,
        text="sim",
    )

    # 'sim' sent during IDLE was discarded as a booking action confirmation
    assert "confirmado" not in resp
    assert "Sou o assistente da Northside Barbers" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value


@pytest.mark.unit
async def test_fast_path_negative_cancels_awaiting_confirmation_flow() -> None:
    """A fast-path 'não' during AWAITING_CONFIRMATION cancels the flow and resets to IDLE."""
    conv_service, uow_factory, _clock, _, _, svc_id, _ = _create_unit_fixtures()
    phone = "+5511999991115"

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(svc_id),
                "service_name": "Corte de Cabelo",
                "action_id": str(uuid.uuid4()),
            },
        )

    resp = await conv_service.handle_message(
        message_id="msg_nao_cancel",
        phone=phone,
        text="não",
    )
    assert "não fiz nenhum agendamento" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}


@pytest.mark.unit
async def test_unrecognized_service_prompts_active_catalog() -> None:
    """When a requested service cannot be resolved, prompt customer with catalog services."""
    conv_service, _, _clock, intent_parser, _, _, _ = _create_unit_fixtures()
    phone = "+5511999991116"

    intent_parser._responses["quero manicure"] = BookingIntent(
        intent=IntentType.BOOK,
        service_name="manicure",
    )

    resp = await conv_service.handle_message(
        message_id="msg_unknown_service",
        phone=phone,
        text="quero manicure",
    )
    assert "Não encontrei esse serviço" in resp
    assert "Corte de Cabelo (R$ 45,00)" in resp


@pytest.mark.unit
async def test_unrecognized_barber_prompts_active_barbers() -> None:
    """When a requested barber cannot be resolved, prompt customer with available barbers."""
    conv_service, _, _clock, intent_parser, _, _svc_id, _ = _create_unit_fixtures()
    phone = "+5511999991117"

    intent_parser._responses["com o zezinho"] = BookingIntent(
        intent=IntentType.BOOK,
        service_name="Corte de Cabelo",
        barber_name="zezinho",
    )

    resp = await conv_service.handle_message(
        message_id="msg_unknown_barber",
        phone=phone,
        text="com o zezinho",
    )
    assert "Não encontrei esse profissional" in resp
    assert "Carlos Barber" in resp


@pytest.mark.unit
async def test_disambiguate_multiple_appointments_for_cancellation() -> None:
    """When customer with multiple future bookings cancels, prompt to choose by number."""
    conv_service, uow_factory, _clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999991118"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Daniel", phone=phone)
        # Create 2 upcoming appointments
        a1 = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=datetime(2026, 11, 21, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 11, 21, 14, 30, tzinfo=UTC),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        a2 = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=datetime(2026, 11, 22, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 11, 22, 14, 30, tzinfo=UTC),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    intent_parser._responses["quero cancelar"] = BookingIntent(intent=IntentType.CANCEL)

    # 1. Turn 1: Bot lists appointments
    resp1 = await conv_service.handle_message(
        message_id="msg_cancel_multi_1",
        phone=phone,
        text="quero cancelar",
    )
    assert "Você possui os seguintes agendamentos futuros" in resp1
    assert "1. Corte de Cabelo" in resp1
    assert "2. Corte de Cabelo" in resp1

    # 2. Turn 2: User selects option "1"
    resp2 = await conv_service.handle_message(
        message_id="msg_cancel_multi_2",
        phone=phone,
        text="o primeiro",
    )
    assert "Deseja realmente cancelar seu agendamento de Corte de Cabelo" in resp2

    # 3. Turn 3: User confirms "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_cancel_multi_3",
        phone=phone,
        text="sim",
    )
    assert "foi cancelado com sucesso" in resp3

    # Verify a1 is cancelled while a2 remains confirmed
    async with uow_factory() as uow:
        appt1 = await uow.appointments.get_by_id(a1.id)
        appt2 = await uow.appointments.get_by_id(a2.id)
        assert appt1 is not None
        assert appt1.status == AppointmentStatus.CANCELLED.value
        assert appt2 is not None
        assert appt2.status == AppointmentStatus.CONFIRMED.value


@pytest.mark.unit
async def test_llm_invalid_output_error_returns_clarification_prompt() -> None:
    """When intent parser raises LLMInvalidOutputError, reply with CLARIFICATION_PROMPT."""
    conv_service, uow_factory, _, _intent_parser, _, _, _ = _create_unit_fixtures()
    phone = "+5511999991119"

    class FailingParser:
        async def parse(self, user_message: str) -> BookingIntent:
            raise LLMInvalidOutputError("Invalid schema from model refusal")

    conv_service._intent_parser = FailingParser()

    resp = await conv_service.handle_message(
        message_id="msg_invalid_llm",
        phone=phone,
        text="mensagem estranha",
    )
    assert "Desculpe, não consegui entender bem a sua mensagem" in resp

    # Verify message was marked as done so retries are not required
    async with uow_factory() as uow:
        msg = await uow.processed_messages.get("msg_invalid_llm")
        assert msg is not None
        assert msg.status == "done"


@pytest.mark.unit
def test_is_valid_customer_name_validation() -> None:
    """Validate customer name rules: characters, length, tokens, and command words."""
    # Valid customer names (accents, spaces, hyphens, apostrophes)
    assert is_valid_customer_name("Maria Clara") is True
    assert is_valid_customer_name("João D'Ávila") is True
    assert is_valid_customer_name("Jean-Luc") is True
    assert is_valid_customer_name("Luiz Inácio") is True
    assert is_valid_customer_name("Renan Maciel Santos") is True

    # Questions, commands, and numbers must NEVER be accepted as names
    assert is_valid_customer_name("na verdade pode ser às 16h?") is False
    assert is_valid_customer_name("qual o valor?") is False
    assert is_valid_customer_name("qual o valor") is False
    assert is_valid_customer_name("quanto custa?") is False
    assert is_valid_customer_name("quanto custa") is False
    assert is_valid_customer_name("Carlos 123") is False
    assert is_valid_customer_name("horarios disponiveis") is False

    # Confirmation and negation tokens must be rejected
    assert is_valid_customer_name("sim") is False
    assert is_valid_customer_name("não") is False
    assert is_valid_customer_name("ok") is False
    assert is_valid_customer_name("cancela") is False

    # Length boundaries (2 to 60 characters)
    assert is_valid_customer_name("A") is False
    assert is_valid_customer_name("   ") is False
    assert is_valid_customer_name("-" * 10) is False
    assert is_valid_customer_name("A" * 61) is False


@pytest.mark.unit
async def test_customer_name_only_accepted_when_awaiting_and_valid() -> None:
    """Verify customer name is only accepted when awaiting, and invalid replies re-prompt."""
    conv_service, uow_factory, _clock, intent_parser, _, svc_id, _ = _create_unit_fixtures()
    phone = "+5511999991120"

    # Pre-seed state: service, date, and time are chosen; bot is awaiting customer_name
    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS.value,
            slots={
                "flow": "book",
                "service_id": str(svc_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "service_duration_minutes": 30,
                "local_date": "2026-11-21",
                "local_time": "15:00:00",
                "awaiting": "customer_name",
            },
        )

    # 1. User replies with a time change question: "na verdade pode ser às 16h?"
    intent_parser._responses["na verdade pode ser às 16h?"] = BookingIntent(
        intent=IntentType.BOOK,
        local_time=time(16, 0),
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_name_reject_1",
        phone=phone,
        text="na verdade pode ser às 16h?",
    )
    # Must re-prompt for customer name; question was NOT saved as customer name
    assert "qual é o seu nome" in resp1
    async with uow_factory() as uow:
        st1 = await uow.conversation_states.get(phone)
        assert st1 is not None
        assert "customer_name" not in st1.slots
        assert st1.slots.get("local_time") == "16:00:00"
        assert st1.slots.get("awaiting") == "customer_name"

    # 2. User asks for price: "qual o valor?"
    resp2 = await conv_service.handle_message(
        message_id="msg_name_reject_2",
        phone=phone,
        text="qual o valor?",
    )
    assert "qual é o seu nome" in resp2
    async with uow_factory() as uow:
        st2 = await uow.conversation_states.get(phone)
        assert st2 is not None
        assert "customer_name" not in st2.slots
        assert st2.slots.get("awaiting") == "customer_name"

    # 3. User provides valid name: "Maria Clara"
    resp3 = await conv_service.handle_message(
        message_id="msg_name_accept_3",
        phone=phone,
        text="Maria Clara",
    )
    # Advanced to confirmation!
    assert "Podemos confirmar?" in resp3
    assert "Maria Clara" in resp3
    async with uow_factory() as uow:
        st3 = await uow.conversation_states.get(phone)
        assert st3 is not None
        assert st3.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st3.slots.get("customer_name") == "Maria Clara"
        assert "awaiting" not in st3.slots


@pytest.mark.unit
async def test_unexpected_exception_marks_message_as_failed_and_reraises() -> None:
    """When _process_turn raises an unexpected error, mark message failed and re-raise."""
    conv_service, uow_factory, _, _, _, _, _ = _create_unit_fixtures()
    phone = "+5511999991121"
    msg_id = "msg_unexpected_fail"

    class ExplodingError(RuntimeError):
        pass

    with (
        patch.object(
            conv_service,
            "_process_turn",
            side_effect=ExplodingError("Simulated infrastructure catastrophe"),
        ),
        pytest.raises(ExplodingError),
    ):
        await conv_service.handle_message(
            message_id=msg_id,
            phone=phone,
            text="Olá",
        )

    # Message must be marked failed to clear lease
    async with uow_factory() as uow:
        msg = await uow.processed_messages.get(msg_id)
        assert msg is not None
        assert msg.status == "failed"


@pytest.mark.unit
async def test_pode_cancelar_during_booking_confirmation_aborts_without_booking() -> None:
    """When customer says 'pode cancelar' during new booking, abort without booking."""
    conv_service, uow_factory, _, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999991199"
    action_id = uuid.uuid4()

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(svc_id),
                "service_name": "Corte de Cabelo",
                "barber_id": str(barber_id),
                "local_date": "2026-11-21",
                "local_time": "15:00:00",
                "customer_name": "Cliente Teste",
                "action_id": str(action_id),
            },
        )

    resp = await conv_service.handle_message(
        message_id="msg_pode_cancelar_booking",
        phone=phone,
        text="pode cancelar",
    )

    # Must abort request (negative outcome), NOT book
    assert "não fiz nenhum agendamento" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}

        # No appointment must exist
        cust = await uow.customers.get_by_phone(phone)
        if cust is not None:
            appts = await uow.appointments.list_by_customer(cust.id)
            assert len(appts) == 0


@pytest.mark.unit
async def test_pode_cancelar_during_cancellation_confirmation_cancels_appointment() -> None:
    """When customer says 'pode cancelar' during cancellation, cancel appointment."""
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999991198"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cancelamento", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    # 1. Turn 1: Ask to cancel
    intent_parser._responses["quero cancelar"] = BookingIntent(intent=IntentType.CANCEL)
    resp1 = await conv_service.handle_message(
        message_id="msg_cancel_ask",
        phone=phone,
        text="quero cancelar",
    )
    assert "Deseja realmente cancelar seu agendamento" in resp1

    # 2. Turn 2: Confirm cancellation using 'pode cancelar'
    resp2 = await conv_service.handle_message(
        message_id="msg_cancel_confirm",
        phone=phone,
        text="pode cancelar",
    )
    assert "foi cancelado com sucesso" in resp2

    async with uow_factory() as uow:
        appt_after = await uow.appointments.get_by_id(appt.id)
        assert appt_after is not None
        assert appt_after.status == AppointmentStatus.CANCELLED.value

        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value


@pytest.mark.unit
@pytest.mark.parametrize("affirmative_token", ["confirmado", "confirmada"])
async def test_confirmado_and_confirmada_remain_affirmative_for_booking(
    affirmative_token: str,
) -> None:
    """'confirmado' and 'confirmada' remain valid affirmative tokens that confirm a booking."""
    conv_service, uow_factory, _, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = f"+5511999991{abs(hash(affirmative_token)) % 10000:04d}"
    action_id = uuid.uuid4()

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": "book",
                "service_id": str(svc_id),
                "service_name": "Corte de Cabelo",
                "barber_id": str(barber_id),
                "local_date": "2026-11-21",
                "local_time": "15:00:00",
                "customer_name": "Cliente Confirmado",
                "action_id": str(action_id),
            },
        )

    resp = await conv_service.handle_message(
        message_id=f"msg_confirm_{affirmative_token}",
        phone=phone,
        text=affirmative_token,
    )
    assert "está confirmado" in resp

    async with uow_factory() as uow:
        cust = await uow.customers.get_by_phone(phone)
        assert cust is not None
        appts = await uow.appointments.list_by_customer(cust.id)
        assert len(appts) == 1
        assert appts[0].status == AppointmentStatus.CONFIRMED.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_idle_cancel() -> None:
    """Entering AWAITING_CONFIRMATION from IDLE with single appointment
    and cancel intent persists CANCEL flow.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992001"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cancel Single", phone=phone)
        await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    intent_parser._responses["quero cancelar"] = BookingIntent(intent=IntentType.CANCEL)
    resp = await conv_service.handle_message(
        message_id="msg_test_flow_1",
        phone=phone,
        text="quero cancelar",
    )
    assert "Deseja realmente cancelar seu agendamento" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.CANCEL.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_idle_reschedule_with_datetime() -> None:
    """Entering AWAITING_CONFIRMATION from IDLE with single appointment
    and reschedule datetime persists RESCHEDULE flow.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992002"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Reschedule Single", phone=phone)
        await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    intent_parser._responses["remarcar para amanha as 14h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=date(2026, 11, 21),
        local_time=time(14, 0),
    )
    resp = await conv_service.handle_message(
        message_id="msg_test_flow_2",
        phone=phone,
        text="remarcar para amanha as 14h",
    )
    assert "Podemos confirmar a remarcação" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.RESCHEDULE.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_choosing_cancel() -> None:
    """Entering AWAITING_CONFIRMATION from CHOOSING_APPOINTMENT for cancel
    flow persists CANCEL flow.
    """
    conv_service, uow_factory, clock, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992003"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Multi Cancel", phone=phone)
        appt1 = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.CHOOSING_APPOINTMENT.value,
            slots={
                "flow": ConversationFlow.CANCEL.value,
                "candidate_appointment_ids": [str(appt1.id)],
            },
        )

    resp = await conv_service.handle_message(
        message_id="msg_test_flow_3",
        phone=phone,
        text="1",
    )
    assert "Deseja realmente cancelar seu agendamento" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.CANCEL.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_choosing_reschedule_with_datetime() -> None:
    """Entering AWAITING_CONFIRMATION from CHOOSING_APPOINTMENT for reschedule
    with datetime persists RESCHEDULE flow.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992004"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Multi Reschedule", phone=phone)
        appt1 = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.CHOOSING_APPOINTMENT.value,
            slots={
                "flow": ConversationFlow.RESCHEDULE.value,
                "candidate_appointment_ids": [str(appt1.id)],
            },
        )

    intent_parser._responses["1 para amanha as 14h"] = BookingIntent(
        local_date=date(2026, 11, 21),
        local_time=time(14, 0),
    )
    resp = await conv_service.handle_message(
        message_id="msg_test_flow_4",
        phone=phone,
        text="1 para amanha as 14h",
    )
    assert "Podemos confirmar a remarcação" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.RESCHEDULE.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_collecting_reschedule() -> None:
    """Entering AWAITING_CONFIRMATION from COLLECTING_BOOKING_DETAILS with reschedule
    datetime persists RESCHEDULE flow.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992005"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Collecting Reschedule", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS.value,
            slots={
                "flow": ConversationFlow.RESCHEDULE.value,
                "appointment_id": str(appt.id),
                "service_name": "Corte de Cabelo",
            },
        )

    intent_parser._responses["amanha as 15h"] = BookingIntent(
        local_date=date(2026, 11, 21),
        local_time=time(15, 0),
    )
    resp = await conv_service.handle_message(
        message_id="msg_test_flow_5",
        phone=phone,
        text="amanha as 15h",
    )
    assert "Podemos confirmar a remarcação" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.RESCHEDULE.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_offering_alternatives() -> None:
    """Entering AWAITING_CONFIRMATION from OFFERING_ALTERNATIVES after picking an alternative
    persists the flow.
    """
    conv_service, uow_factory, clock, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992006"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Alternatives", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        alt_dt = clock.now() + timedelta(days=2)
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.OFFERING_ALTERNATIVES.value,
            slots={
                "flow": ConversationFlow.RESCHEDULE.value,
                "appointment_id": str(appt.id),
                "service_name": "Corte de Cabelo",
                "alternative_slots": [alt_dt.isoformat()],
            },
        )

    resp = await conv_service.handle_message(
        message_id="msg_test_flow_6",
        phone=phone,
        text="1",
    )
    assert "Podemos confirmar a remarcação" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.RESCHEDULE.value


@pytest.mark.unit
async def test_awaiting_confirmation_persists_flow_collecting_booking() -> None:
    """Entering AWAITING_CONFIRMATION from COLLECTING_BOOKING_DETAILS for booking flow
    persists BOOK flow.
    """
    conv_service, uow_factory, _, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992007"

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS.value,
            slots={
                "flow": ConversationFlow.BOOK.value,
                "service_id": str(svc_id),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "service_duration_minutes": 30,
                "barber_id": str(barber_id),
                "barber_name": "Carlos Barber",
                "local_date": "2026-11-21",
                "local_time": "14:00:00",
                "awaiting": "customer_name",
            },
        )

    resp = await conv_service.handle_message(
        message_id="msg_test_flow_7",
        phone=phone,
        text="Roberto Carlos",
    )
    assert "Podemos confirmar? Agendamento de" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.BOOK.value


@pytest.mark.unit
async def test_awaiting_confirmation_missing_flow_fails_safe_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """State in AWAITING_CONFIRMATION without flow fails safe on 'sim', logs error,
    and executes nothing.
    """
    conv_service, uow_factory, _, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999992008"
    action_id = uuid.uuid4()

    async with uow_factory() as uow:
        # Seed AWAITING_CONFIRMATION state with action_id but MISSING 'flow'
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "action_id": str(action_id),
                "service_id": str(svc_id),
                "barber_id": str(barber_id),
                "local_date": "2026-11-21",
                "local_time": "14:00:00",
                "customer_name": "Cliente Sem Flow",
            },
        )

    with caplog.at_level(logging.ERROR):
        resp = await conv_service.handle_message(
            message_id="msg_test_missing_flow",
            phone=phone,
            text="sim",
        )

    # 1. Neutral template asking customer to restart/redo request
    assert resp == render_template(TemplateKey.GREETING_HELP)

    # 2. State reset to IDLE and slots emptied
    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}

        # 3. No appointment was created or executed
        cust = await uow.customers.get_by_phone(phone)
        if cust is not None:
            appts = await uow.appointments.list_by_customer(cust.id)
            assert len(appts) == 0

    # 4. Structured log event 'missing_pending_flow' logged at ERROR level
    error_logs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.ERROR and getattr(rec, "event", None) == "missing_pending_flow"
    ]
    assert getattr(error_logs[0], "conversation_id", None) == mask_phone(phone)
    assert not hasattr(error_logs[0], "phone")
    assert getattr(error_logs[0], "stage", None) == ConversationStage.AWAITING_CONFIRMATION.value


@pytest.mark.unit
def test_mask_phone_privacy() -> None:
    """mask_phone preserves country/area prefix and last 4 digits while masking the rest."""
    phone = "+5511999998888"
    masked = mask_phone(phone)
    assert masked == "+5511*****8888"
    assert phone[-4:] in masked
    assert "99999" not in masked

    # Very short strings are fully masked with asterisks
    assert mask_phone("12345678") == "********"
    assert mask_phone("1234") == "****"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate", "flow", "expected"),
    [
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.BOOK, True),
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.RESCHEDULE, True),
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.CANCEL, True),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.BOOK, False),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.RESCHEDULE, False),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.CANCEL, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.BOOK, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.RESCHEDULE, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.CANCEL, True),
    ],
)
def test_resolve_confirmation_truth_table(
    candidate: ConfirmationCandidate,
    flow: ConversationFlow,
    expected: bool,
) -> None:
    """Parametrized verification of all 9 combinations in the confirmation truth table."""
    assert resolve_confirmation(candidate, flow) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "ambiguous_phrase",
    [
        "não pode cancelar",
        "nao pode cancelar",
        "não cancela",
        "cancela não",
        "não, pode cancelar",
        "pode cancelar sim",
    ],
)
def test_ambiguous_cancellation_phrases_do_not_produce_fast_path_candidate(
    ambiguous_phrase: str,
) -> None:
    """Phrases containing mixed or negated intent must not be classified
    as fast-path candidates.
    """
    assert classify_fast_path_candidate(ambiguous_phrase) is None


@pytest.mark.unit
async def test_cancel_confirmation_negative_keeps_active_and_avoids_cancelad() -> None:
    """Saying 'não' in AWAITING_CONFIRMATION for cancel flow keeps appointment CONFIRMED,
    returns the flow-specific message, and does not contain the word 'cancelad'.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999993001"

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Nao Cancela", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=clock.now() + timedelta(days=1),
            ends_at=clock.now() + timedelta(days=1, minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    # 1. Turn 1: Ask to cancel -> advances to AWAITING_CONFIRMATION with flow=CANCEL
    intent_parser._responses["quero cancelar"] = BookingIntent(intent=IntentType.CANCEL)
    resp1 = await conv_service.handle_message(
        message_id="msg_cancel_ask_no",
        phone=phone,
        text="quero cancelar",
    )
    assert "Deseja realmente cancelar seu agendamento" in resp1

    # 2. Turn 2: User says "não" to back out
    resp2 = await conv_service.handle_message(
        message_id="msg_cancel_say_no",
        phone=phone,
        text="não",
    )

    # Verify flow-specific back-out response
    assert resp2 == "Tudo bem, não cancelei nada: seu agendamento continua confirmado."
    assert "cancelad" not in resp2.lower()

    # Verify appointment remains CONFIRMED in the repository
    async with uow_factory() as uow:
        appt_after = await uow.appointments.get_by_id(appt.id)
        assert appt_after is not None
        assert appt_after.status == AppointmentStatus.CONFIRMED.value

        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert st.slots == {}


@pytest.mark.unit
@pytest.mark.parametrize("intent_type", list(IntentType))
@pytest.mark.parametrize("has_updates", [False, True])
async def test_unrecognized_reply_during_cancel_confirmation_never_executes(
    intent_type: IntentType,
    has_updates: bool,
) -> None:
    """Verify that no LLM intent (with or without extracted slot updates) can trigger
    execution while awaiting confirmation of a cancellation.

    If slot updates are provided, verify the pending cancellation converts into
    a reschedule confirmation, and confirming with 'sim' executes the reschedule
    without creating duplicate appointments or unwanted cancellations.
    """
    conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999994001"
    target_dt = clock.now() + timedelta(days=1)

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Teste", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=target_dt,
            ends_at=target_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        action_id = uuid.uuid4()
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={
                "flow": ConversationFlow.CANCEL.value,
                "appointment_id": str(appt.id),
                "action_id": str(action_id),
                "service_id": str(svc_id),
                "barber_id": str(barber_id),
                "service_name": "Corte de Cabelo",
                "barber_name": "Carlos Barber",
                "local_date": target_dt.date().isoformat(),
                "local_time": target_dt.time().isoformat(),
            },
        )

    unrecognized_text = "não pode cancelar"
    if has_updates:
        intent_parser._responses[unrecognized_text] = BookingIntent(
            intent=intent_type,
            local_date=target_dt.date() + timedelta(days=1),
            local_time=time(14, 0),
        )
    else:
        intent_parser._responses[unrecognized_text] = BookingIntent(
            intent=intent_type,
        )

    turn_result = await conv_service.process_turn(
        message_id="msg_cancel_disambig",
        phone=phone,
        text=unrecognized_text,
    )

    # Prove that the turn did NOT execute the pending cancellation
    assert turn_result.stage_to != ConversationStage.EXECUTING.value

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        appt_after = await uow.appointments.get_by_id(appt.id)

    assert st is not None
    if not has_updates:
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert turn_result.response_text == render_template(TemplateKey.CLARIFICATION_PROMPT)
        # Crucial invariant: original appointment remains active and confirmed in the repository
        assert appt_after is not None
        assert appt_after.status == AppointmentStatus.CONFIRMED
    else:
        assert st.stage == ConversationStage.AWAITING_CONFIRMATION.value
        assert st.slots.get("flow") == ConversationFlow.RESCHEDULE.value
        assert turn_result.response_text is not None
        assert "Podemos confirmar a remarcação" in turn_result.response_text

        # Turn 2: Send "sim" to confirm the reschedule
        yes_resp = await conv_service.handle_message(
            message_id=f"msg_cancel_confirm_yes_{intent_type.value}",
            phone=phone,
            text="sim",
        )
        assert yes_resp is not None
        assert "remarcado com sucesso" in yes_resp

        async with uow_factory() as uow:
            all_appts = await uow.appointments.list_by_customer(cust.id)
            appt_final = await uow.appointments.get_by_id(appt.id)

        # Invariants: no duplicate appointments, no cancellation unconfirmed by user
        assert len(all_appts) == 1
        assert appt_final is not None
        assert appt_final.id == appt.id
        assert appt_final.status == AppointmentStatus.CONFIRMED.value
        expected_new_dt = localize_datetime(
            target_dt.date() + timedelta(days=1),
            time(14, 0),
            ZoneInfo(settings.business_timezone),
        )
        assert appt_final.starts_at == expected_new_dt


@pytest.mark.unit
async def test_scenario_a_reschedule_with_hour_change_confirms_reschedule_and_executes() -> None:
    """Scenario A: Client requests reschedule, in confirmation asks for 17h, confirms with 'sim'.
    Verify exactly 1 appointment remains, moved to 17:00, and confirmation prompt was reschedule.
    """
    conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999995001"
    tomorrow_date = clock.now().date() + timedelta(days=1)
    business_tz = ZoneInfo(settings.business_timezone)
    initial_start_dt = localize_datetime(tomorrow_date, time(14, 0), business_tz)

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cenário A", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=initial_start_dt,
            ends_at=initial_start_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    # Turn 1: Client initiates reschedule to tomorrow at 15h
    intent_parser._responses["remarcar para amanha as 15h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(15, 0),
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_scen_a_1",
        phone=phone,
        text="remarcar para amanha as 15h",
    )
    assert resp1 is not None
    assert "Podemos confirmar a remarcação" in resp1
    assert "15:00" in resp1

    # Turn 2: Client changes time during confirmation: "melhor às 17h"
    intent_parser._responses["melhor às 17h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_time=time(17, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_scen_a_2",
        phone=phone,
        text="melhor às 17h",
    )
    assert resp2 is not None
    # Confirmation prompt displayed is of reschedule
    assert "Podemos confirmar a remarcação" in resp2
    assert "17:00" in resp2

    # Turn 3: Client confirms with "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_scen_a_3",
        phone=phone,
        text="sim",
    )
    assert resp3 is not None
    assert "remarcado com sucesso" in resp3
    assert "17:00" in resp3

    # Database verification
    async with uow_factory() as uow:
        all_appts = await uow.appointments.list_by_customer(cust.id)
        appt_final = await uow.appointments.get_by_id(appt.id)

    assert len(all_appts) == 1
    assert appt_final is not None
    assert appt_final.id == appt.id
    assert appt_final.status == AppointmentStatus.CONFIRMED.value
    expected_17h = localize_datetime(tomorrow_date, time(17, 0), business_tz)
    assert appt_final.starts_at == expected_17h


@pytest.mark.unit
async def test_scenario_b_cancel_with_new_datetime_converts_to_reschedule_and_executes() -> None:
    """Scenario B: Client asks to cancel, in confirmation specifies new date/time, confirms.
    Verify exactly 1 appointment remains, rescheduled to new date/time (not cancelled, no dup).
    """
    conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999995002"
    tomorrow_date = clock.now().date() + timedelta(days=1)
    target_new_date = tomorrow_date + timedelta(days=1)
    business_tz = ZoneInfo(settings.business_timezone)
    initial_start_dt = localize_datetime(tomorrow_date, time(14, 0), business_tz)

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cenário B", phone=phone)
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=initial_start_dt,
            ends_at=initial_start_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    # Turn 1: Client asks to cancel
    intent_parser._responses["quero cancelar meu agendamento"] = BookingIntent(
        intent=IntentType.CANCEL,
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_scen_b_1",
        phone=phone,
        text="quero cancelar meu agendamento",
    )
    assert resp1 is not None
    assert "Deseja realmente cancelar seu agendamento" in resp1

    # Turn 2: Client specifies new date/time during cancel confirmation
    update_text = "prefiro mudar para quinta-feira às 10h"
    intent_parser._responses[update_text] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=target_new_date,
        local_time=time(10, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_scen_b_2",
        phone=phone,
        text=update_text,
    )
    assert resp2 is not None
    # Confirmation prompt displayed is of reschedule ("de <original> para <novo>")
    assert "Podemos confirmar a remarcação" in resp2
    assert "10:00" in resp2

    # Turn 3: Client confirms with "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_scen_b_3",
        phone=phone,
        text="sim",
    )
    assert resp3 is not None
    assert "remarcado com sucesso" in resp3

    # Database verification
    async with uow_factory() as uow:
        all_appts = await uow.appointments.list_by_customer(cust.id)
        appt_final = await uow.appointments.get_by_id(appt.id)

    assert len(all_appts) == 1
    assert appt_final is not None
    assert appt_final.id == appt.id
    assert appt_final.status == AppointmentStatus.CONFIRMED.value
    expected_new_dt = localize_datetime(target_new_date, time(10, 0), business_tz)
    assert appt_final.starts_at == expected_new_dt


@pytest.mark.unit
async def test_scenario_c_book_with_time_change_confirms_and_executes() -> None:
    """Scenario C: Client creates booking, changes time during confirmation, confirms with 'sim'.
    Verify exactly 1 appointment exists, at the new time.
    """
    conv_service, uow_factory, clock, intent_parser, settings, _svc_id, _barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999995003"
    tomorrow_date = clock.now().date() + timedelta(days=1)
    business_tz = ZoneInfo(settings.business_timezone)

    async with uow_factory() as uow:
        await uow.customers.create(name="Maria", phone=phone)

    # Turn 1: Client initiates booking for tomorrow at 14h
    booking_init_text = "quero agendar corte amanhã às 14h com Carlos Barber"
    intent_parser._responses[booking_init_text] = BookingIntent(
        intent=IntentType.BOOK,
        service_name="Corte de Cabelo",
        local_date=tomorrow_date,
        local_time=time(14, 0),
        barber_name="Carlos Barber",
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_scen_c_1",
        phone=phone,
        text=booking_init_text,
    )
    assert resp1 is not None
    assert "Podemos confirmar? Agendamento de Corte" in resp1
    assert "14:00" in resp1

    # Turn 2: Client changes time during booking confirmation: "mudar para às 16h"
    intent_parser._responses["mudar para às 16h"] = BookingIntent(
        intent=IntentType.BOOK,
        local_time=time(16, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_scen_c_2",
        phone=phone,
        text="mudar para às 16h",
    )
    assert resp2 is not None
    assert "Podemos confirmar? Agendamento de Corte" in resp2
    assert "16:00" in resp2

    # Turn 3: Client confirms with "sim"
    resp3 = await conv_service.handle_message(
        message_id="msg_scen_c_3",
        phone=phone,
        text="sim",
    )
    assert resp3 is not None
    assert "Seu horário para Corte de Cabelo está confirmado" in resp3
    assert "16:00" in resp3

    # Database verification
    async with uow_factory() as uow:
        cust = await uow.customers.get_by_phone(phone)
        assert cust is not None
        all_appts = await uow.appointments.list_by_customer(cust.id)

    assert len(all_appts) == 1
    assert all_appts[0].status == AppointmentStatus.CONFIRMED.value
    expected_16h = localize_datetime(tomorrow_date, time(16, 0), business_tz)
    assert all_appts[0].starts_at == expected_16h


@pytest.mark.unit
async def test_scenario_d_reschedule_unavailable_chooses_alt_confirms_executes() -> None:
    """Scenario D: Client reschedules, chooses occupied slot, receives alts, picks alt 1, confirms.
    Verify exactly 1 appointment remains, rescheduled to the chosen alternative.
    """
    conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999995004"
    other_phone = "+5511999995005"
    tomorrow_date = clock.now().date() + timedelta(days=1)
    business_tz = ZoneInfo(settings.business_timezone)
    initial_start_dt = localize_datetime(tomorrow_date, time(14, 0), business_tz)
    occupied_dt = localize_datetime(tomorrow_date, time(15, 0), business_tz)

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cenário D", phone=phone)
        other_cust = await uow.customers.create(name="Outro Cliente", phone=other_phone)
        # Client's original appointment at 14:00
        appt = await uow.appointments.create(
            customer_id=cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=initial_start_dt,
            ends_at=initial_start_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )
        # Occupied slot by another client at 15:00
        await uow.appointments.create(
            customer_id=other_cust.id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=occupied_dt,
            ends_at=occupied_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED,
        )

    # Turn 1: Client enters reschedule confirmation
    intent_parser._responses["remarcar para amanha as 14h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(14, 0),
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_scen_d_1",
        phone=phone,
        text="remarcar para amanha as 14h",
    )
    assert resp1 is not None
    assert "Podemos confirmar a remarcação" in resp1

    # Turn 2: Client changes time to 15h, which is occupied
    intent_parser._responses["melhor às 15h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_time=time(15, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_scen_d_2",
        phone=phone,
        text="melhor às 15h",
    )
    assert resp2 is not None
    assert "já está ocupado" in resp2
    assert "Temos estes horários disponíveis" in resp2

    # Turn 3: Client picks alternative 1
    resp3 = await conv_service.handle_message(
        message_id="msg_scen_d_3",
        phone=phone,
        text="1",
    )
    assert resp3 is not None
    assert "Podemos confirmar a remarcação" in resp3

    # Turn 4: Client confirms with "sim"
    resp4 = await conv_service.handle_message(
        message_id="msg_scen_d_4",
        phone=phone,
        text="sim",
    )
    assert resp4 is not None
    assert "remarcado com sucesso" in resp4

    # Database verification
    async with uow_factory() as uow:
        all_appts = await uow.appointments.list_by_customer(cust.id)
        appt_final = await uow.appointments.get_by_id(appt.id)

    assert len(all_appts) == 1
    assert appt_final is not None
    assert appt_final.id == appt.id
    assert appt_final.status == AppointmentStatus.CONFIRMED.value
    assert appt_final.starts_at != initial_start_dt
    assert appt_final.starts_at != occupied_dt


@pytest.mark.unit
@pytest.mark.parametrize(
    ("flow", "expected_template_fragment"),
    [
        (ConversationFlow.BOOK, "Podemos confirmar? Agendamento de"),
        (ConversationFlow.RESCHEDULE, "Podemos confirmar a remarcação de"),
        (ConversationFlow.CANCEL, "Deseja realmente cancelar seu agendamento"),
    ],
)
async def test_awaiting_confirmation_invariant_matches_flow_template(
    flow: ConversationFlow,
    expected_template_fragment: str,
) -> None:
    """Parameterized invariant for the 3 flows: when entering AWAITING_CONFIRMATION,
    the template displayed strictly corresponds to the recorded flow.
    """
    conv_service, _, _, _, _, _, _ = _create_unit_fixtures()
    slots: dict[str, Any] = {
        "service_name": "Corte de Cabelo",
        "barber_name": "Carlos Barber",
        "local_date": "2026-10-25",
        "local_time": "14:00:00",
        "old_date_time": "24/10 às 10:00",
        "customer_name": "Cliente Teste",
        "service_price_cents": 4500,
    }

    stage, out_slots, prompt = conv_service._enter_confirmation(flow=flow, slots=slots)

    assert stage == ConversationStage.AWAITING_CONFIRMATION.value
    assert out_slots.get("flow") == flow.value
    assert "action_id" in out_slots
    assert expected_template_fragment in prompt
