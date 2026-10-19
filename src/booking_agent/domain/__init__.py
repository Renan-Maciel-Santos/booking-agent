"""Domain models, ports, and errors."""

from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.enums import AppointmentStatus, MessageStatus
from booking_agent.domain.errors import (
    AppointmentNotFoundError,
    BarberNotFoundError,
    ConversationStateNotFoundError,
    CustomerNotFoundError,
    DomainError,
    NotFoundError,
    ServiceNotFoundError,
    SlotUnavailableError,
)
from booking_agent.domain.ports import DatabaseHealthChecker, IntentParser

__all__ = [
    "AppointmentNotFoundError",
    "AppointmentStatus",
    "BarberNotFoundError",
    "BookingIntent",
    "ConversationStateNotFoundError",
    "CustomerNotFoundError",
    "DatabaseHealthChecker",
    "DomainError",
    "IntentParser",
    "IntentType",
    "MessageStatus",
    "NotFoundError",
    "ServiceNotFoundError",
    "SlotUnavailableError",
]
