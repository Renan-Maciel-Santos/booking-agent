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
