"""Domain models and serialization for conversation state."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from booking_agent.domain.enums import ConversationFlow, ConversationStage

ANY_BARBER: Final = "any"


class ConversationSlots(BaseModel):
    """Typed domain representation of conversation slot state.

    Maintains exact JSON key names used by the persistence layer while enforcing
    domain types and forbidding unknown attributes.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    flow: ConversationFlow | None = None
    action_id: UUID | None = None
    executing_message_id: str | None = Field(default=None, min_length=1)
    awaiting: Literal["service", "date_time", "customer_name"] | None = None
    service_id: UUID | None = None
    service_name: str | None = None
    service_price_cents: int | None = Field(default=None, ge=0)
    barber_id: UUID | Literal["any"] | None = None
    barber_name: str | None = None
    local_date: date | None = None
    local_time: time | None = None
    customer_name: str | None = None
    appointment_id: UUID | None = None
    old_date_time: str | None = None
    candidate_appointment_ids: list[UUID] | None = None
    alternative_slots: list[AwareDatetime] | None = None

    def barber_filter(self) -> UUID | None:
        """Extract optional barber UUID filter from slots, returning None for 'any' or unset.

        Returns:
            Specific barber UUID, or None if barber_id is None or ANY_BARBER.
        """
        bid = self.barber_id
        if bid is None or bid == ANY_BARBER:
            return None
        return bid


def slots_from_json(data: dict[str, Any]) -> ConversationSlots:
    """Validate and deserialize raw JSON dictionary into typed ConversationSlots.

    Args:
        data: Raw dictionary loaded from repository JSONB storage.

    Returns:
        Validated ConversationSlots instance.
    """
    return ConversationSlots.model_validate(data)


def slots_to_json(slots: ConversationSlots) -> dict[str, Any]:
    """Serialize typed ConversationSlots to a dictionary suitable for JSONB storage.

    Uses exclude_none so unpopulated slots (represented as None) are omitted
    from the serialized dictionary, as None strictly means 'not set'.

    Args:
        slots: Typed ConversationSlots instance.

    Returns:
        Dictionary representation conforming to database JSONB format.
    """
    return slots.model_dump(mode="json", exclude_none=True)


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Immutable domain representation of a conversation's persisted state."""

    stage: ConversationStage
    slots: ConversationSlots
    updated_at: datetime
