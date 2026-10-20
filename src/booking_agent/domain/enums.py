"""Domain enumerations for entity and message lifecycles."""

from enum import StrEnum


class AppointmentStatus(StrEnum):
    """Lifecycle status of an appointment."""

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class MessageStatus(StrEnum):
    """Processing state for idempotency-tracked messages."""

    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ActionKind(StrEnum):
    """Supported business action kinds for idempotency tracking."""

    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"


class ConversationStage(StrEnum):
    """Explicit stages of the conversation state machine."""

    IDLE = "idle"
    COLLECTING_BOOKING_DETAILS = "collecting_booking_details"
    OFFERING_ALTERNATIVES = "offering_alternatives"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    CHOOSING_APPOINTMENT = "choosing_appointment"
    COMPLETED = "completed"
