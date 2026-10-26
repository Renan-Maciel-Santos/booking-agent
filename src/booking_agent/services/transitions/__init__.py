"""State transitions package for the conversation service state machine."""

from booking_agent.services.transitions.collecting import (
    handle_collecting_booking_details,
    is_valid_customer_name,
)
from booking_agent.services.transitions.confirmation import (
    ConfirmationDetails,
    enter_confirmation,
    extract_pending_flow,
    handle_missing_confirmation_data,
    load_reschedule_origin,
    mask_phone,
    validate_booking_slots_and_confirm,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.dispatch import (
    STAGE_HANDLERS,
    StageHandler,
    UnhandledStageError,
)
from booking_agent.services.transitions.idle import handle_idle
from booking_agent.services.transitions.reads import ConversationReads, UnitOfWorkReads
from booking_agent.services.transitions.transition import Transition

__all__ = [
    "STAGE_HANDLERS",
    "ConfirmationDetails",
    "ConversationReads",
    "StageHandler",
    "Transition",
    "TransitionContext",
    "UnhandledStageError",
    "UnitOfWorkReads",
    "enter_confirmation",
    "extract_pending_flow",
    "handle_collecting_booking_details",
    "handle_idle",
    "handle_missing_confirmation_data",
    "is_valid_customer_name",
    "load_reschedule_origin",
    "mask_phone",
    "validate_booking_slots_and_confirm",
]
