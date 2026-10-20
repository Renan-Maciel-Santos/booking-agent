"""Structured output the LLM must produce: the user's booking intent."""

from datetime import date, time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class IntentType(StrEnum):
    """What the user wants to do."""

    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


class TimePreference(StrEnum):
    """Broad time-of-day preference when no specific hour is requested."""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


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
    # Local calendar date (YYYY-MM-DD) in the barbershop's business timezone.
    local_date: date | None = None
    # Local wall-clock time (HH:MM:SS) in the barbershop's business timezone.
    local_time: time | None = None
    # Optional broad period of day when exact hour is omitted (e.g. "tomorrow morning").
    time_preference: TimePreference | None = None
    # Preferred barber name if stated by the customer; mapped to active barbers by the service.
    barber_name: str | None = Field(default=None, min_length=1, max_length=100)
    # Customer name if self-introduced during dialogue.
    customer_name: str | None = Field(default=None, min_length=1, max_length=100)
