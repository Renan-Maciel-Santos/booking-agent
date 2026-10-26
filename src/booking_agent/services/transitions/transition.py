"""Typed transition result for conversation state machine stage transitions."""

from dataclasses import dataclass

from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationStage


@dataclass(frozen=True)
class Transition:
    """Outcome of a conversation state transition.

    Attributes:
        stage: Next ConversationStage enum member.
        slots: State slots resulting from the transition.
        reply: Rendered text response to return to the customer.
    """

    stage: ConversationStage
    slots: ConversationSlots
    reply: str
