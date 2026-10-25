"""Domain models and serialization for conversation state."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from booking_agent.domain.enums import ConversationFlow, ConversationStage


class ConversationSlots(BaseModel):
    """Typed domain representation of conversation slot state.

    Maintains exact JSON key names used by the persistence layer while enforcing
    domain types and forbidding unknown attributes.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    flow: ConversationFlow | None = None
    action_id: UUID | None = None
    executing_message_id: str | None = None
    awaiting: Literal["service", "date_time", "customer_name"] | None = None
    service_id: UUID | None = None
    service_name: str | None = None
    service_price_cents: int | None = Field(default=None, ge=0)
    barber_id: UUID | None = None
    barber_name: str | None = None
    local_date: date | None = None
    local_time: time | None = None
    customer_name: str | None = None
    appointment_id: UUID | None = None
    old_date_time: str | None = None
    candidate_appointment_ids: list[UUID] | None = None
    alternative_slots: list[AwareDatetime] | None = None


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

    Uses exclude_unset so only explicitly set keys (including explicit None values)
    are serialized, preserving sentinel values like barber_id=None.

    Args:
        slots: Typed ConversationSlots instance.

    Returns:
        Dictionary representation conforming to database JSONB format.
    """
    return slots.model_dump(mode="json", exclude_unset=True)


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Immutable domain representation of a conversation's persisted state."""

    stage: ConversationStage
    slots: ConversationSlots
    updated_at: datetime
