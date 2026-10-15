"""Structured output the LLM must produce: the user's booking intent."""

# StrEnum gives JSON-friendly enum values.
from enum import StrEnum

# UUIDs identify existing appointments.
from uuid import UUID

# AwareDatetime rejects naive datetimes; ConfigDict tightens validation; Field adds constraints.
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class IntentType(StrEnum):
    """What the user wants to do."""

    # Create a new appointment.
    BOOK = "book"
    # Move an existing appointment.
    RESCHEDULE = "reschedule"
    # Cancel an existing appointment.
    CANCEL = "cancel"
    # Anything the parser could not classify.
    UNKNOWN = "unknown"


class BookingIntent(BaseModel):
    """Intent and entities extracted from a single user message.

    This is a *parsing* result only. It carries no guarantee that the slot exists,
    is free, or that the appointment belongs to the user; those checks belong to
    the deterministic service layer.
    """

    # Immutable, and unknown keys from the LLM are rejected instead of silently dropped.
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Classified intent; UNKNOWN is the safe default.
    intent: IntentType = IntentType.UNKNOWN
    # Free-text service name as the user said it; mapped to a catalog entry later.
    service_name: str | None = Field(default=None, min_length=1, max_length=100)
    # Requested start time; must include a timezone to avoid ambiguous slots.
    requested_start: AwareDatetime | None = None
    # Existing appointment referenced by reschedule/cancel requests.
    appointment_id: UUID | None = None
