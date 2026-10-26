"""Unit tests for stage dispatching and unhandled stage handling."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationStage
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.dispatch import (
    ORCHESTRATOR_OWNED_STAGES,
    STAGE_HANDLERS,
    UnhandledStageError,
    dispatch_transition,
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
def transition_context(base_clock: FakeClock, business_tz: ZoneInfo) -> TransitionContext:
    """Create a minimal TransitionContext for dispatch tests."""
    return TransitionContext(
        reads=InMemoryReads(),
        clock=base_clock,
        entity_resolver=EntityResolver(),
        business_tz=business_tz,
        phone="+5511999998888",
    )


@pytest.mark.unit
def test_dispatch_table_contains_expected_stages() -> None:
    """Extracted decision stages are in STAGE_HANDLERS, with COMPLETED aliased to IDLE."""
    assert ConversationStage.IDLE in STAGE_HANDLERS
    assert ConversationStage.COMPLETED in STAGE_HANDLERS
    assert ConversationStage.COLLECTING_BOOKING_DETAILS in STAGE_HANDLERS
    assert ConversationStage.CHOOSING_APPOINTMENT in STAGE_HANDLERS
    assert ConversationStage.OFFERING_ALTERNATIVES in STAGE_HANDLERS
    assert ConversationStage.AWAITING_CONFIRMATION in STAGE_HANDLERS

    # COMPLETED receives identical handler function as IDLE
    assert STAGE_HANDLERS[ConversationStage.COMPLETED] is STAGE_HANDLERS[ConversationStage.IDLE]


@pytest.mark.unit
def test_dispatch_table_covers_all_conversation_stages_exhaustively() -> None:
    """Every ConversationStage is registered in STAGE_HANDLERS or ORCHESTRATOR_OWNED_STAGES."""
    all_stages = set(ConversationStage)
    handler_stages = set(STAGE_HANDLERS.keys())

    assert all_stages == handler_stages | ORCHESTRATOR_OWNED_STAGES
    assert handler_stages & ORCHESTRATOR_OWNED_STAGES == set()


@pytest.mark.unit
async def test_awaiting_confirmation_stage_registered_and_handled(
    transition_context: TransitionContext,
) -> None:
    """AWAITING_CONFIRMATION stage is registered in STAGE_HANDLERS and handled."""
    slots = ConversationSlots(flow=None)

    transition = await dispatch_transition(
        stage=ConversationStage.AWAITING_CONFIRMATION.value,
        ctx=transition_context,
        slots=slots,
        text="sim",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE


@pytest.mark.unit
async def test_completed_stage_dispatches_to_idle_handler(
    transition_context: TransitionContext,
) -> None:
    """COMPLETED stage is handled identically to IDLE via STAGE_HANDLERS dispatch."""
    slots = ConversationSlots()

    transition = await dispatch_transition(
        stage=ConversationStage.COMPLETED.value,
        ctx=transition_context,
        slots=slots,
        text="olá",
        intent=None,
    )

    assert transition.stage == ConversationStage.IDLE


@pytest.mark.unit
async def test_dispatch_transition_with_executing_raises_unhandled_stage_error(
    transition_context: TransitionContext,
) -> None:
    """EXECUTING stage reaching dispatch_transition raises UnhandledStageError."""
    slots = ConversationSlots()

    with pytest.raises(UnhandledStageError) as exc_info:
        await dispatch_transition(
            stage=ConversationStage.EXECUTING.value,
            ctx=transition_context,
            slots=slots,
            text="confirmar",
            intent=None,
        )

    assert exc_info.value.stage == ConversationStage.EXECUTING.value
