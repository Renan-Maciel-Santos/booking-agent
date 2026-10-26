"""Context container for state transition evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from booking_agent.domain.ports import Clock
from booking_agent.services.entity_resolver import EntityResolver
from booking_agent.services.transitions.reads import ConversationReads


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """Read-only context and dependencies required by transition stage handlers."""

    reads: ConversationReads
    clock: Clock
    entity_resolver: EntityResolver
    business_tz: ZoneInfo
    phone: str
