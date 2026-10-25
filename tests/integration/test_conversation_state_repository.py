"""Integration tests for ConversationStateRepository against real PostgreSQL."""

import logging
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.config import Settings
from booking_agent.database.models import Appointment, ConversationState
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.conversation_state import (
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import ConversationFlow, ConversationStage
from booking_agent.domain.errors import InvalidConversationStateError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.repositories.conversation_state_repository import ConversationStateRepository
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import ConversationService, mask_phone
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.templates import TemplateKey, render_template
from tests.fakes.fake_clock import FakeClock


def _random_phone() -> str:
    """Generate a valid numeric E.164 phone number."""
    return f"+1555{secrets.randbelow(9000000) + 1000000}"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("stage", "slots_data"),
    [
        (
            ConversationStage.COLLECTING_BOOKING_DETAILS,
            {
                "service_id": str(uuid.uuid4()),
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "barber_id": str(uuid.uuid4()),
                "barber_name": "Carlos Barber",
                "local_date": "2026-11-21",
                "local_time": "14:00:00",
                "customer_name": "Renan Maciel",
            },
        ),
        (
            ConversationStage.COLLECTING_BOOKING_DETAILS,
            {
                "service_id": str(uuid.uuid4()),
                "barber_id": None,
                "local_date": "2026-11-21",
                "local_time": "14:00:00",
            },
        ),
        (
            ConversationStage.CHOOSING_APPOINTMENT,
            {
                "appointment_id": str(uuid.uuid4()),
                "old_date_time": "20/11 às 14:00",
            },
        ),
        (
            ConversationStage.OFFERING_ALTERNATIVES,
            {
                "candidate_appointment_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
                "alternative_slots": ["2026-11-21T14:00:00+00:00"],
                "awaiting": "service",
            },
        ),
        (
            ConversationStage.AWAITING_CONFIRMATION,
            {
                "flow": ConversationFlow.BOOK.value,
                "action_id": str(uuid.uuid4()),
            },
        ),
        (
            ConversationStage.EXECUTING,
            {
                "executing_message_id": "msg_exec_12345",
            },
        ),
    ],
)
async def test_conversation_slots_roundtrip_through_database_for_each_adr_group(
    session_factory: async_sessionmaker[AsyncSession],
    stage: ConversationStage,
    slots_data: dict[str, object],
) -> None:
    """Upsert and get round-trip through real PostgreSQL JSONB for all ADR 0008 slot groups."""
    phone = _random_phone()
    expected_slots = slots_from_json(slots_data)

    async with session_factory() as session:
        repo = ConversationStateRepository(session)
        upserted = await repo.upsert(phone=phone, stage=stage, slots=expected_slots)
        await session.commit()

        assert upserted.stage == stage
        assert upserted.slots == expected_slots
        assert slots_to_json(upserted.slots) == slots_to_json(expected_slots)

    async with session_factory() as session:
        repo = ConversationStateRepository(session)
        loaded = await repo.get(phone)
        assert loaded is not None
        assert loaded.stage == stage
        assert loaded.slots == expected_slots
        assert slots_to_json(loaded.slots) == slots_to_json(expected_slots)


@pytest.mark.integration
async def test_direct_invalid_json_row_raises_invalid_conversation_state_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row with invalid JSON inserted directly raises InvalidConversationStateError on get."""
    phone = _random_phone()
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        invalid_row = ConversationState(
            phone=phone,
            stage=ConversationStage.IDLE.value,
            slots={"unknown_key_xyz": "SENSITIVE_VALUE_999"},
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(invalid_row)
        await session.commit()

    async with session_factory() as session:
        repo = ConversationStateRepository(session)
        with pytest.raises(InvalidConversationStateError) as exc_info:
            await repo.get(phone)

        assert "unknown_key_xyz" in exc_info.value.invalid_fields
        assert "unknown_key_xyz" in str(exc_info.value)
        assert "SENSITIVE_VALUE_999" not in str(exc_info.value)


@pytest.mark.integration
async def test_direct_invalid_stage_row_raises_invalid_conversation_state_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row with unknown stage inserted directly raises InvalidConversationStateError on get."""
    phone = _random_phone()
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        invalid_row = ConversationState(
            phone=phone,
            stage="collecting_info",
            slots={},
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(invalid_row)
        await session.commit()

    async with session_factory() as session:
        repo = ConversationStateRepository(session)
        with pytest.raises(InvalidConversationStateError) as exc_info:
            await repo.get(phone)

        assert "stage" in exc_info.value.invalid_fields
        assert "stage" in str(exc_info.value)


@pytest.mark.integration
async def test_direct_invalid_stage_and_slots_raises_both_in_invalid_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When both stage and slots are invalid, both field names appear in invalid_fields."""
    phone = _random_phone()
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        invalid_row = ConversationState(
            phone=phone,
            stage="collecting_info",
            slots={"unknown_slot_key": "val"},
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(invalid_row)
        await session.commit()

    async with session_factory() as session:
        repo = ConversationStateRepository(session)
        with pytest.raises(InvalidConversationStateError) as exc_info:
            await repo.get(phone)

        assert "stage" in exc_info.value.invalid_fields
        assert "unknown_slot_key" in exc_info.value.invalid_fields


@pytest.mark.integration
async def test_invalid_persisted_stage_resets_and_unblocks_second_message(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An invalid persisted stage resets to IDLE, and a subsequent message is processed normally."""
    phone = _random_phone()
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        invalid_row = ConversationState(
            phone=phone,
            stage="collecting_info",
            slots={},
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(invalid_row)
        await session.commit()

    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    # First message: triggers recovery, responds with CONVERSATION_RESET, resets state to IDLE
    with caplog.at_level(logging.ERROR):
        resp1 = await conv_service.handle_message(
            message_id="msg_reset_stage_1",
            phone=phone,
            text="Oi, quero agendar",
        )

    assert resp1 == render_template(TemplateKey.CONVERSATION_RESET)

    async with session_factory() as session:
        row = await session.get(ConversationState, phone)
        assert row is not None
        assert row.stage == ConversationStage.IDLE.value
        assert row.slots == {}

    # Second message: conversation is not stuck; processes normally from IDLE
    resp2 = await conv_service.handle_message(
        message_id="msg_reset_stage_2",
        phone=phone,
        text="Quero cortar o cabelo",
    )

    assert resp2 != render_template(TemplateKey.CONVERSATION_RESET)
    assert len(resp2) > 0

    async with session_factory() as session:
        row2 = await session.get(ConversationState, phone)
        assert row2 is not None
        assert row2.stage != ConversationStage.IDLE.value


@pytest.mark.integration
async def test_invalid_persisted_state_with_client_message_resets_conversation_and_masks_pii(
    session_factory: async_sessionmaker[AsyncSession],
    integration_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persisted invalid state fails safe: resets to IDLE, sends CONVERSATION_RESET, masks PII."""
    phone = _random_phone()
    now_utc = datetime.now(UTC)
    marker = "SENSITIVE_PII_MARKER_99999"

    async with session_factory() as session:
        invalid_row = ConversationState(
            phone=phone,
            stage=ConversationStage.AWAITING_CONFIRMATION.value,
            slots={"unrecognized_field": marker},
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(invalid_row)
        await session.commit()

    clock = FakeClock(now_utc)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    booking_service = BookingService(uow_factory, clock, integration_settings)
    intent_parser = FakeIntentParser(clock=clock)
    entity_resolver = EntityResolver()
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=integration_settings,
    )

    with caplog.at_level(logging.ERROR):
        resp = await conv_service.handle_message(
            message_id="msg_reset_integration_test",
            phone=phone,
            text="Quero marcar um corte",
        )

    # 1. Response is the dedicated CONVERSATION_RESET template
    assert resp == render_template(TemplateKey.CONVERSATION_RESET)

    # 2. State in database is reset to IDLE and empty slots
    async with session_factory() as session:
        row = await session.get(ConversationState, phone)
        assert row is not None
        assert row.stage == ConversationStage.IDLE.value
        assert row.slots == {}

        # 3. No appointment was created
        appts = (await session.execute(select(Appointment))).scalars().all()
        assert len(appts) == 0

    # 4. Structured log event 'invalid_conversation_state' without sensitive value
    invalid_logs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.ERROR
        and getattr(rec, "event", None) == "invalid_conversation_state"
    ]
    assert len(invalid_logs) == 1
    assert getattr(invalid_logs[0], "phone_last4", None) == mask_phone(phone)
    assert "unrecognized_field" in getattr(invalid_logs[0], "invalid_fields", ())

    for rec in caplog.records:
        assert marker not in rec.getMessage()
        assert marker not in str(getattr(rec, "extra", {}))
