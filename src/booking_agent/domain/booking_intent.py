"""Structured output the LLM must produce: the user's booking intent."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class IntentType(StrEnum):
    """What the user wants to do."""

    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


class BookingIntent(BaseModel):
    """Intent and entities extracted from a single user message.

    This is a *parsing* result only. It carries no guarantee that the slot exists,
    is free, or that the appointment belongs to the user; those checks belong to
    the deterministic service layer.
    """

    # extra="forbid": fields invented by the LLM are rejected, not silently dropped.
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: IntentType = IntentType.UNKNOWN
    # Free text as the user said it; mapped to a catalog entry by the service.
    service_name: str | None = Field(default=None, min_length=1, max_length=100)
    # AwareDatetime rejects naive datetimes, which would make slots ambiguous.
    requested_start: AwareDatetime | None = None
    appointment_id: UUID | None = None
