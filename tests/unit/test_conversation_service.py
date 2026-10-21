"""Unit tests for ConversationService state machine and orchestration with fakes."""

import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest

from booking_agent.config import Settings
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.enums import AppointmentStatus, ConversationStage
from booking_agent.domain.errors import LLMInvalidOutputError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import (
    ConversationService,
    is_valid_customer_name,
)
from booking_agent.services.entity_resolver import EntityResolver
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
    assert "solicitação foi cancelada" in resp

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
