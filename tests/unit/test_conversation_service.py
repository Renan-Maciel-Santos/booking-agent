"""Unit tests for ConversationService state machine and orchestration with fakes."""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from booking_agent.config import Settings
from booking_agent.database.models import Appointment, Customer
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.conversation_state import (
    ConversationSlots,
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConfirmationCandidate,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import LLMInvalidOutputError, MissingConfirmationDataError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.services.booking_service import BookingService, localize_datetime
from booking_agent.services.conversation_service import (
    ConfirmationDetails,
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
from tests.fakes.fake_uow import (
    FakeConversationStateRepository,
    FakeUnitOfWorkFactory,
    InMemoryDataStore,
)


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
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots_from_json({"service_id": str(svc_id)}),
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
        assert slots_to_json(st.slots) == {}


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
            stage=ConversationStage.IDLE,
            slots=slots_from_json({}),
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
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "flow": "book",
                    "service_id": str(svc_id),
                    "service_name": "Corte de Cabelo",
                    "action_id": str(uuid.uuid4()),
                }
            ),
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
        assert slots_to_json(st.slots) == {}


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
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots_from_json(
                {
                    "flow": "book",
                    "service_id": str(svc_id),
                    "service_name": "Corte de Cabelo",
                    "service_price_cents": 4500,
                    "local_date": "2026-11-21",
                    "local_time": "15:00:00",
                    "awaiting": "customer_name",
                }
            ),
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
        st1_slots = slots_to_json(st1.slots)
        assert "customer_name" not in st1_slots
        assert st1_slots.get("local_time") == "16:00:00"
        assert st1_slots.get("awaiting") == "customer_name"

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
        st2_slots = slots_to_json(st2.slots)
        assert "customer_name" not in st2_slots
        assert st2_slots.get("awaiting") == "customer_name"

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
        st3_slots = slots_to_json(st3.slots)
        assert st3_slots.get("customer_name") == "Maria Clara"
        assert "awaiting" not in st3_slots


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
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "flow": "book",
                    "service_id": str(svc_id),
                    "service_name": "Corte de Cabelo",
                    "barber_id": str(barber_id),
                    "local_date": "2026-11-21",
                    "local_time": "15:00:00",
                    "customer_name": "Cliente Teste",
                    "action_id": str(action_id),
                }
            ),
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
        assert slots_to_json(st.slots) == {}

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
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "flow": "book",
                    "service_id": str(svc_id),
                    "service_name": "Corte de Cabelo",
                    "barber_id": str(barber_id),
                    "local_date": "2026-11-21",
                    "local_time": "15:00:00",
                    "customer_name": "Cliente Confirmado",
                    "service_price_cents": 4500,
                    "action_id": str(action_id),
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.CANCEL.value


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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.RESCHEDULE.value


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
            stage=ConversationStage.CHOOSING_APPOINTMENT,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.CANCEL.value,
                    "candidate_appointment_ids": [str(appt1.id)],
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.CANCEL.value


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
            stage=ConversationStage.CHOOSING_APPOINTMENT,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.RESCHEDULE.value,
                    "candidate_appointment_ids": [str(appt1.id)],
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.RESCHEDULE.value


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
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.RESCHEDULE.value,
                    "appointment_id": str(appt.id),
                    "service_name": "Corte de Cabelo",
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.RESCHEDULE.value


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
            stage=ConversationStage.OFFERING_ALTERNATIVES,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.RESCHEDULE.value,
                    "appointment_id": str(appt.id),
                    "service_name": "Corte de Cabelo",
                    "alternative_slots": [alt_dt.isoformat()],
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.RESCHEDULE.value


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
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.BOOK.value,
                    "service_id": str(svc_id),
                    "service_name": "Corte de Cabelo",
                    "service_price_cents": 4500,
                    "barber_id": str(barber_id),
                    "barber_name": "Carlos Barber",
                    "local_date": "2026-11-21",
                    "local_time": "14:00:00",
                    "awaiting": "customer_name",
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.BOOK.value


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
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "action_id": str(action_id),
                    "service_id": str(svc_id),
                    "barber_id": str(barber_id),
                    "local_date": "2026-11-21",
                    "local_time": "14:00:00",
                    "customer_name": "Cliente Sem Flow",
                }
            ),
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
        assert slots_to_json(st.slots) == {}

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
    assert getattr(error_logs[0], "phone_masked", None) == mask_phone(phone)
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
        assert slots_to_json(st.slots) == {}


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
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.CANCEL.value,
                    "appointment_id": str(appt.id),
                    "action_id": str(action_id),
                    "service_id": str(svc_id),
                    "barber_id": str(barber_id),
                    "service_name": "Corte de Cabelo",
                    "barber_name": "Carlos Barber",
                    "local_date": target_dt.date().isoformat(),
                    "local_time": target_dt.time().isoformat(),
                }
            ),
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
        assert slots_to_json(st.slots).get("flow") == ConversationFlow.RESCHEDULE.value
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
async def test_changing_time_during_reschedule_confirmation_moves_the_same_appointment() -> None:
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
async def test_new_datetime_during_cancel_confirmation_becomes_a_reschedule() -> None:
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
async def test_changing_time_during_booking_confirmation_books_the_new_time() -> None:
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
async def test_unavailable_reschedule_alternative_keeps_the_original_appointment_flow() -> None:
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
async def test_cancel_turned_reschedule_through_unavailable_alternatives() -> None:
    """Client in cancel confirmation sends unavailable datetime, picks alternative, and confirms.

    Verifies cancel converts into reschedule through alternative offering, exactly 1 appointment
    remains (rescheduled to the chosen alternative with status confirmed), and the confirmation
    prompt displayed is for rescheduling.
    """
    conv_service, uow_factory, clock, intent_parser, settings, svc_id, barber_id = (
        _create_unit_fixtures()
    )
    phone = "+5511999996001"
    other_phone = "+5511999996002"
    tomorrow_date = clock.now().date() + timedelta(days=1)
    business_tz = ZoneInfo(settings.business_timezone)
    initial_start_dt = localize_datetime(tomorrow_date, time(10, 0), business_tz)
    occupied_dt = localize_datetime(tomorrow_date, time(15, 0), business_tz)

    async with uow_factory() as uow:
        cust = await uow.customers.create(name="Cliente Cancel to Alt", phone=phone)
        other_cust = await uow.customers.create(name="Outro Cliente", phone=other_phone)
        # Client's original confirmed appointment at 10:00
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

    # Turn 1: Client enters cancel confirmation
    intent_parser._responses["quero cancelar"] = BookingIntent(
        intent=IntentType.CANCEL,
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_cancel_alt_1",
        phone=phone,
        text="quero cancelar",
    )
    assert resp1 is not None
    assert "Deseja realmente cancelar seu agendamento" in resp1

    # Turn 2: Client changes mind and sends new date/time that is occupied
    intent_parser._responses["melhor amanhã às 15h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=tomorrow_date,
        local_time=time(15, 0),
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_cancel_alt_2",
        phone=phone,
        text="melhor amanhã às 15h",
    )
    assert resp2 is not None
    assert "já está ocupado" in resp2
    assert "Temos estes horários disponíveis" in resp2

    # Turn 3: Client chooses alternative 1
    resp3 = await conv_service.handle_message(
        message_id="msg_cancel_alt_3",
        phone=phone,
        text="1",
    )
    assert resp3 is not None
    assert "Podemos confirmar a remarcação" in resp3
    assert "Deseja realmente cancelar" not in resp3
    assert "Agendamento de" not in resp3

    # Turn 4: Client confirms with "sim"
    resp4 = await conv_service.handle_message(
        message_id="msg_cancel_alt_4",
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
    valid_slots: dict[str, Any] = {
        "service_name": "Corte de Cabelo",
        "barber_name": "Carlos Barber",
        "local_date": "2026-10-25",
        "local_time": "14:00:00",
        "old_date_time": "24/10 às 10:00",
        "customer_name": "Cliente Teste",
        "service_price_cents": 4500,
    }
    slots = slots_from_json(valid_slots)
    details = ConfirmationDetails.from_slots(slots, ZoneInfo("America/Sao_Paulo"))
    transition = conv_service._enter_confirmation(flow=flow, slots=slots, details=details)
    stage, out_slots, prompt = transition.stage.value, transition.slots, transition.reply

    assert stage == ConversationStage.AWAITING_CONFIRMATION.value
    assert out_slots.flow == flow.value
    assert out_slots.action_id is not None
    assert expected_template_fragment in prompt


@pytest.mark.unit
def test_enter_confirmation_with_explicit_typed_details() -> None:
    """Verify that _enter_confirmation accepts explicit typed ConfirmationDetails."""
    conv_service, _, _, _, _, _, _ = _create_unit_fixtures()
    slots = ConversationSlots()
    details = ConfirmationDetails(
        service="Barba Terapia",
        date="26/10",
        time="16:00",
        barber="Marcos Barber",
        name="Carlos",
        price="R$ 55,00",
        old_date_time="25/10 às 15:00",
    )
    transition = conv_service._enter_confirmation(
        flow=ConversationFlow.RESCHEDULE,
        slots=slots,
        details=details,
    )
    stage, out_slots, prompt = transition.stage.value, transition.slots, transition.reply
    assert stage == ConversationStage.AWAITING_CONFIRMATION.value
    assert out_slots.flow == ConversationFlow.RESCHEDULE.value
    assert out_slots.action_id is not None
    assert "Barba Terapia" in prompt
    assert "25/10 às 15:00" in prompt
    assert "26/10" in prompt
    assert "16:00" in prompt


@pytest.mark.unit
@pytest.mark.parametrize("flow", [ConversationFlow.RESCHEDULE, ConversationFlow.CANCEL])
@pytest.mark.parametrize(
    "reason", ["missing_id", "invalid_id", "not_found", "not_confirmed", "not_owned"]
)
async def test_invalid_reschedule_origin_fails_safe_to_idle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flow: ConversationFlow,
    reason: str,
) -> None:
    """Parametric test verifying that any invalid reschedule origin in RESCHEDULE
    and CANCEL flows fails safe to IDLE, clears slots, renders a neutral message,
    and executes no mutations.
    """
    conv_service, uow_factory, clock, intent_parser, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999990001"
    now_utc = clock.now()
    now_dt = now_utc + timedelta(days=2)

    cust_id = uuid.uuid4()
    uow_factory.store.customers[cust_id] = Customer(
        id=cust_id,
        phone=phone,
        name="Cliente Origem",
        created_at=now_utc,
        updated_at=now_utc,
    )

    other_cust_id = uuid.uuid4()
    uow_factory.store.customers[other_cust_id] = Customer(
        id=other_cust_id,
        phone="+5511999990002",
        name="Outro Cliente",
        created_at=now_utc,
        updated_at=now_utc,
    )

    slots: dict[str, Any] = {
        "flow": flow.value,
        "action_id": str(uuid.uuid4()),
        "service_id": str(svc_id),
        "barber_id": str(barber_id),
        "local_date": now_dt.date().isoformat(),
        "local_time": "14:00:00",
    }

    if reason == "missing_id":
        pass
    elif reason == "invalid_id":
        slots["appointment_id"] = "not-a-valid-uuid"
    elif reason == "not_found":
        slots["appointment_id"] = str(uuid.uuid4())
    elif reason == "not_confirmed":
        appt_cancelled_id = uuid.uuid4()
        uow_factory.store.appointments[appt_cancelled_id] = Appointment(
            id=appt_cancelled_id,
            customer_id=cust_id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=now_dt,
            ends_at=now_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CANCELLED.value,
            created_at=now_utc,
            updated_at=now_utc,
        )
        slots["appointment_id"] = str(appt_cancelled_id)
    elif reason == "not_owned":
        appt_other_id = uuid.uuid4()
        uow_factory.store.appointments[appt_other_id] = Appointment(
            id=appt_other_id,
            customer_id=other_cust_id,
            service_id=svc_id,
            barber_id=barber_id,
            starts_at=now_dt,
            ends_at=now_dt + timedelta(minutes=30),
            price_cents=4500,
            status=AppointmentStatus.CONFIRMED.value,
            created_at=now_utc,
            updated_at=now_utc,
        )
        slots["appointment_id"] = str(appt_other_id)

    if reason == "invalid_id":
        invalid_slots = ConversationSlots()
        object.__setattr__(invalid_slots, "appointment_id", "not-a-valid-uuid")
        async with uow_factory() as uow:
            with caplog.at_level(logging.ERROR):
                origin = await conv_service._load_reschedule_origin(uow, phone, invalid_slots)
            assert origin is None
            error_logs = [
                rec
                for rec in caplog.records
                if rec.levelno == logging.ERROR
                and getattr(rec, "event", None) == "invalid_reschedule_origin"
            ]
            assert len(error_logs) == 1
            assert getattr(error_logs[0], "reason", None) == "invalid_id"
            assert getattr(error_logs[0], "phone_masked", None) == mask_phone(phone)
        return

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(slots),
        )

    mock_book = AsyncMock()
    mock_reschedule = AsyncMock()
    mock_cancel = AsyncMock()
    monkeypatch.setattr(conv_service._booking_service, "book", mock_book)
    monkeypatch.setattr(conv_service._booking_service, "reschedule", mock_reschedule)
    monkeypatch.setattr(conv_service._booking_service, "cancel", mock_cancel)

    intent_parser._responses["melhor às 16h"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_time=time(16, 0),
    )

    with caplog.at_level(logging.ERROR):
        resp = await conv_service.handle_message(
            message_id="msg_test_invalid_origin",
            phone=phone,
            text="melhor às 16h",
        )

    assert resp == render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND)
    assert "Não consegui localizar esse agendamento" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert slots_to_json(st.slots) == {}

    mock_book.assert_not_called()
    mock_reschedule.assert_not_called()
    mock_cancel.assert_not_called()
    assert len(uow_factory.store.booking_actions) == 0

    error_logs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.ERROR
        and getattr(rec, "event", None) == "invalid_reschedule_origin"
    ]
    assert len(error_logs) == 1
    assert getattr(error_logs[0], "reason", None) == reason
    assert getattr(error_logs[0], "phone_masked", None) == mask_phone(phone)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("missing_key", "expected_missing_field"),
    [
        ("service_name", "service"),
        ("local_date", "date"),
        ("local_time", "time"),
        ("customer_name", "name"),
        ("service_price_cents", "price"),
    ],
)
def test_confirmation_details_from_slots_missing_field_raises_error(
    missing_key: str,
    expected_missing_field: str,
) -> None:
    """from_slots without each mandatory field raises MissingConfirmationDataError listing it."""
    valid_slots: dict[str, Any] = {
        "service_name": "Corte de Cabelo",
        "local_date": "2026-10-25",
        "local_time": "14:00:00",
        "customer_name": "Cliente Teste",
        "service_price_cents": 4500,
        "barber_name": "Carlos Barber",
    }
    valid_slots.pop(missing_key)
    with pytest.raises(MissingConfirmationDataError) as exc_info:
        ConfirmationDetails.from_slots(slots_from_json(valid_slots), ZoneInfo("America/Sao_Paulo"))

    assert expected_missing_field in exc_info.value.missing_fields


@pytest.mark.unit
def test_confirmation_details_from_slots_missing_all_fields_lists_all() -> None:
    """from_slots on empty slots lists all missing fields in MissingConfirmationDataError."""
    with pytest.raises(MissingConfirmationDataError) as exc_info:
        ConfirmationDetails.from_slots(ConversationSlots(), ZoneInfo("America/Sao_Paulo"))

    assert exc_info.value.missing_fields == ("service", "date", "time", "name", "price")


@pytest.mark.unit
def test_reschedule_flow_without_old_date_time_raises_error() -> None:
    """_enter_confirmation requires old_date_time for RESCHEDULE and raises if missing."""
    conv_service, _, _, _, _, _, _ = _create_unit_fixtures()
    details = ConfirmationDetails(
        service="Corte",
        date="25/10",
        time="14:00",
        name="Cliente",
        price="R$ 45,00",
        old_date_time=None,
    )
    with pytest.raises(MissingConfirmationDataError) as exc_info:
        conv_service._enter_confirmation(
            flow=ConversationFlow.RESCHEDULE,
            slots=ConversationSlots(),
            details=details,
        )
    assert exc_info.value.missing_fields == ("old_date_time",)


@pytest.mark.unit
async def test_incomplete_slots_in_awaiting_confirmation_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete slots in AWAITING_CONFIRMATION fail safe to IDLE with neutral message."""
    conv_service, uow_factory, _, _, _, svc_id, barber_id = _create_unit_fixtures()
    phone = "+5511999997788"

    async with uow_factory() as uow:
        await uow.conversation_states.upsert(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION,
            slots=slots_from_json(
                {
                    "flow": ConversationFlow.BOOK.value,
                    "action_id": str(uuid.uuid4()),
                    # missing customer_name and service_price_cents
                    "service_id": str(svc_id),
                    "barber_id": str(barber_id),
                    "local_date": "2026-10-25",
                    "local_time": "14:00:00",
                }
            ),
        )

    mock_book = AsyncMock()
    mock_reschedule = AsyncMock()
    mock_cancel = AsyncMock()
    monkeypatch.setattr(conv_service._booking_service, "book", mock_book)
    monkeypatch.setattr(conv_service._booking_service, "reschedule", mock_reschedule)
    monkeypatch.setattr(conv_service._booking_service, "cancel", mock_cancel)

    resp = await conv_service.handle_message(
        message_id="msg_incomplete_slots_confirm",
        phone=phone,
        text="sim",
    )

    assert resp == render_template(TemplateKey.CONFIRMATION_DATA_UNAVAILABLE)
    assert "Tive um problema para montar a confirmação" in resp

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE.value
        assert slots_to_json(st.slots) == {}

    mock_book.assert_not_called()
    mock_reschedule.assert_not_called()
    mock_cancel.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("flow", list(ConversationFlow))
def test_no_confirmation_prompt_contains_zero_price_or_empty_datetime(
    flow: ConversationFlow,
) -> None:
    """Confirmation prompts rendered via _enter_confirmation never contain 'R$ 0,00' or ' às  '."""
    conv_service, _, _, _, _, _, _ = _create_unit_fixtures()
    details = ConfirmationDetails(
        service="Corte Teste",
        date="25/10",
        time="14:00",
        name="Cliente Real",
        price="R$ 50,00",
        barber="Barbeiro Carlos",
        old_date_time="24/10 às 10:00",
    )
    transition = conv_service._enter_confirmation(
        flow=flow,
        slots=slots_from_json({"local_date": "2026-10-25", "local_time": "14:00:00"}),
        details=details,
    )
    prompt = transition.reply
    assert "R$ 0,00" not in prompt
    assert " às  " not in prompt


@pytest.mark.unit
def test_confirmation_details_from_slots_valid_canonical() -> None:
    """Canonical slot keys produce the expected ConfirmationDetails."""
    tz = ZoneInfo("America/Sao_Paulo")
    raw = {
        "service_name": "Corte de Cabelo",
        "local_date": "2026-10-26",
        "local_time": "15:00:00",
        "customer_name": "Renan Maciel",
        "service_price_cents": 4500,
        "barber_name": "Bruno",
    }
    slots = slots_from_json(raw)
    details = ConfirmationDetails.from_slots(slots, tz)
    assert details.service == "Corte de Cabelo"
    assert details.date == "26/10"
    assert details.time == "15:00"
    assert details.name == "Renan Maciel"
    assert details.price == "R$ 45,00"
    assert details.barber == "Bruno"


@pytest.mark.unit
async def test_save_state_with_unknown_key_raises_validation_error() -> None:
    """Invalid slot state during construction indicates a programmer error and fails fast."""
    conv_service, uow_factory, _, _, _, _, _ = _create_unit_fixtures()
    phone = "+5511999998888"

    async with uow_factory() as uow:
        with pytest.raises(ValidationError) as exc_info:
            await conv_service._save_state(
                uow=uow,
                phone=phone,
                stage=ConversationStage.IDLE.value,
                slots=slots_from_json({"unknown_key_123": "value"}),
            )
    assert "unknown_key_123" in str(exc_info.value)


@pytest.mark.unit
async def test_invalid_persisted_stage_resets_conversation_and_unblocks_dialogue() -> None:
    """An unknown persisted stage resets the conversation to IDLE and unblocks the next message."""
    conv_service, uow_factory, _, intent_parser, _, _, _ = _create_unit_fixtures()
    phone = "+5511999998888"

    async with uow_factory() as uow:
        assert isinstance(uow.conversation_states, FakeConversationStateRepository)
        uow.conversation_states.seed_raw(
            phone=phone,
            stage="collecting_info",
            slots={},
        )

    resp1 = await conv_service.handle_message(
        message_id="msg_unknown_stage_1",
        phone=phone,
        text="Oi",
    )
    assert resp1 == render_template(TemplateKey.CONVERSATION_RESET)

    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert st.stage == ConversationStage.IDLE
        assert slots_to_json(st.slots) == {}

    intent_parser._responses["Quero cortar o cabelo"] = BookingIntent(
        intent=IntentType.BOOK,
        service_name="Corte de Cabelo",
    )
    resp2 = await conv_service.handle_message(
        message_id="msg_unknown_stage_2",
        phone=phone,
        text="Quero cortar o cabelo",
    )
    assert resp2 != render_template(TemplateKey.CONVERSATION_RESET)
    assert len(resp2) > 0


@pytest.mark.unit
async def test_booking_with_any_barber_preserves_any_on_reschedule() -> None:
    """Booking with any barber sets barber_id='any' and preserves it across reschedule."""
    conv_service, uow_factory, _, intent_parser, _, _, _ = _create_unit_fixtures()
    phone = "+5511999993333"

    # Add a second active barber so that availability can allocate either barber
    barber_2_id = uuid.uuid4()
    uow_factory.store.barbers[barber_2_id] = type(
        "Brb",
        (),
        {
            "id": barber_2_id,
            "name": "Bruno Barber",
            "active": True,
        },
    )()

    # Turn 1: Customer requests service with date, time, and explicit "qualquer barbeiro"
    intent_parser._responses["Quero cortar o cabelo dia 21/11 às 14:00 com qualquer barbeiro"] = (
        BookingIntent(
            intent=IntentType.BOOK,
            service_name="Corte de Cabelo",
            local_date=date(2026, 11, 21),
            local_time=time(14, 0),
            barber_name="qualquer",
        )
    )
    resp1 = await conv_service.handle_message(
        message_id="msg_bany_1",
        phone=phone,
        text="Quero cortar o cabelo dia 21/11 às 14:00 com qualquer barbeiro",
    )
    assert "nome" in resp1

    # Verify that the persisted slot in fake has barber_id == "any"
    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert slots_to_json(st.slots).get("barber_id") == "any"

    # Turn 2: Customer provides name
    resp2 = await conv_service.handle_message(
        message_id="msg_bany_2",
        phone=phone,
        text="Renan Maciel",
    )
    assert "confirmar" in resp2.lower()

    # Turn 3: Customer confirms booking
    resp3 = await conv_service.handle_message(
        message_id="msg_bany_3",
        phone=phone,
        text="Sim",
    )
    assert "confirmado" in resp3

    # Verify initial appointment was created
    async with uow_factory() as uow:
        cust = await uow.customers.get_by_phone(phone)
        assert cust is not None
        appts = await uow.appointments.list_by_customer(cust.id)
        assert len(appts) == 1

    # Turn 4: Customer requests to reschedule to 2026-11-22 15:00
    intent_parser._responses["Quero remarcar para 22/11 às 15:00"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        local_date=date(2026, 11, 22),
        local_time=time(15, 0),
    )
    resp4 = await conv_service.handle_message(
        message_id="msg_bany_4",
        phone=phone,
        text="Quero remarcar para 22/11 às 15:00",
    )
    assert "confirmar" in resp4.lower()

    # Turn 5: During reschedule confirmation, customer specifies "qualquer barbeiro"
    intent_parser._responses["pode ser com qualquer barbeiro"] = BookingIntent(
        intent=IntentType.RESCHEDULE,
        barber_name="qualquer",
    )
    resp5 = await conv_service.handle_message(
        message_id="msg_bany_5",
        phone=phone,
        text="pode ser com qualquer barbeiro",
    )
    assert "confirmar" in resp5.lower()

    # Verify that the reschedule slot persisted in fake has barber_id == "any"
    async with uow_factory() as uow:
        st = await uow.conversation_states.get(phone)
        assert st is not None
        assert slots_to_json(st.slots).get("barber_id") == "any"

    # Turn 6: Confirm reschedule
    resp6 = await conv_service.handle_message(
        message_id="msg_bany_6",
        phone=phone,
        text="Sim",
    )
    assert "remarcado com sucesso" in resp6
