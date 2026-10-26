"""Unit tests for stage dispatching and unhandled stage handling."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from booking_agent.config import Settings
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationStage
from booking_agent.services.conversation_service import ConversationService
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.transitions.dispatch import (
    STAGE_HANDLERS,
    UnhandledStageError,
)
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
def conv_service(test_settings: Settings, base_clock: FakeClock) -> ConversationService:
    """Create a minimal ConversationService for transition tests."""
    return ConversationService(
        uow_factory=MagicMock(),
        booking_service=MagicMock(),
        intent_parser=MagicMock(),
        entity_resolver=EntityResolver(),
        settings=test_settings,
        clock=base_clock,
    )


@pytest.mark.unit
def test_dispatch_table_contains_expected_stages() -> None:
    """Extracted decision stages are in STAGE_HANDLERS, with COMPLETED aliased to IDLE."""
    assert ConversationStage.IDLE in STAGE_HANDLERS
    assert ConversationStage.COMPLETED in STAGE_HANDLERS
    assert ConversationStage.COLLECTING_BOOKING_DETAILS in STAGE_HANDLERS
    assert ConversationStage.CHOOSING_APPOINTMENT in STAGE_HANDLERS
    assert ConversationStage.OFFERING_ALTERNATIVES in STAGE_HANDLERS

    # COMPLETED receives identical handler function as IDLE
    assert STAGE_HANDLERS[ConversationStage.COMPLETED] is STAGE_HANDLERS[ConversationStage.IDLE]


@pytest.mark.unit
async def test_awaiting_confirmation_still_handled_in_class(
    conv_service: ConversationService,
) -> None:
    """AWAITING_CONFIRMATION stage remains handled in the class."""
    assert ConversationStage.AWAITING_CONFIRMATION not in STAGE_HANDLERS

    reads = InMemoryReads()
    slots = ConversationSlots(flow=None)

    transition = await conv_service._transition_state(
        reads=reads,
        phone="+5511999998888",
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        slots=slots,
        text="sim",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE


@pytest.mark.unit
async def test_completed_stage_dispatches_to_idle_handler(
    conv_service: ConversationService,
) -> None:
    """COMPLETED stage is handled identically to IDLE via STAGE_HANDLERS dispatch."""
    reads = InMemoryReads()
    slots = ConversationSlots()

    transition = await conv_service._transition_state(
        reads=reads,
        phone="+5511999998888",
        stage=ConversationStage.COMPLETED.value,
        slots=slots,
        text="olá",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE


@pytest.mark.unit
async def test_transition_state_with_executing_raises_unhandled_stage_error(
    conv_service: ConversationService,
) -> None:
    """EXECUTING stage reaching _transition_state raises UnhandledStageError."""
    reads = InMemoryReads()
    slots = ConversationSlots()

    with pytest.raises(UnhandledStageError) as exc_info:
        await conv_service._transition_state(
            reads=reads,
            phone="+5511999998888",
            stage=ConversationStage.EXECUTING.value,
            slots=slots,
            text="confirmar",
            intent=None,
        )

    assert exc_info.value.stage == ConversationStage.EXECUTING.value
