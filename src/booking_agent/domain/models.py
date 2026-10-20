"""Domain result models returned by service operations."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from booking_agent.database.models import Appointment
from booking_agent.domain.enums import MessageStatus


@dataclass(frozen=True)
class MessageAcquisition:
    """Outcome of attempting to acquire processing rights for an incoming message."""

    acquired: bool
    status: MessageStatus
    response_text: str | None = None

    def __bool__(self) -> bool:
        """Allow treating acquisition directly as boolean success in conditional checks."""
        return self.acquired


@dataclass(frozen=True)
class AvailabilityResult:
    """Result of querying available appointment slots for a service and date."""

    service_id: uuid.UUID
    target_date: date
    slots: Sequence[datetime]
    barber_id: uuid.UUID | None = None


@dataclass(frozen=True)
class BookingResult:
    """Result of successfully creating a confirmed appointment."""

    appointment: Appointment


@dataclass(frozen=True)
class RescheduleResult:
    """Result of successfully rescheduling an existing appointment."""

    appointment: Appointment


@dataclass(frozen=True)
class CancelResult:
    """Result of cancelling an appointment."""

    appointment: Appointment
