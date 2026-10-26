"""Dispatch table and handler protocols for conversation stage transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationStage
from booking_agent.services.transitions.awaiting_confirmation import (
    handle_awaiting_confirmation,
)
from booking_agent.services.transitions.choosing import handle_choosing_appointment
from booking_agent.services.transitions.collecting import handle_collecting_booking_details
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.idle import handle_idle
from booking_agent.services.transitions.offering import handle_offering_alternatives
from booking_agent.services.transitions.transition import Transition


class UnhandledStageError(Exception):
    """Raised when an unhandled or invalid stage reaches the transition dispatcher."""

    def __init__(self, stage: str | ConversationStage) -> None:
        """Initialize unhandled stage error with stage name.

        Args:
            stage: Stage value or enum member that could not be handled.
        """
        stage_name = stage.value if isinstance(stage, ConversationStage) else str(stage)
        super().__init__(f"Unhandled conversation stage: {stage_name}")
        self.stage = stage_name


class StageHandler(Protocol):
    """Protocol for async stage transition handlers."""

    async def __call__(
        self,
        ctx: TransitionContext,
        slots: ConversationSlots,
        text: str,
        intent: BookingIntent | None,
    ) -> Transition:
        """Execute a state transition for a conversation turn."""
        ...


STAGE_HANDLERS: Mapping[ConversationStage, StageHandler] = {
    ConversationStage.IDLE: handle_idle,
    ConversationStage.COMPLETED: handle_idle,
    ConversationStage.COLLECTING_BOOKING_DETAILS: handle_collecting_booking_details,
    ConversationStage.CHOOSING_APPOINTMENT: handle_choosing_appointment,
    ConversationStage.OFFERING_ALTERNATIVES: handle_offering_alternatives,
    ConversationStage.AWAITING_CONFIRMATION: handle_awaiting_confirmation,
}
