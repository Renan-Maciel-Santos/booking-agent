"""Domain models, ports, and errors."""

from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.enums import AppointmentStatus, MessageStatus
from booking_agent.domain.errors import (
    AppointmentInThePastError,
    AppointmentNotConfirmedError,
    AppointmentNotFoundError,
    BarberNotFoundError,
    ConversationStateNotFoundError,
    CustomerNotFoundError,
    DomainError,
    InvalidLocalTimeError,
    InvalidTimeWindowError,
    MinimumNoticeViolationError,
    NotAppointmentOwnerError,
    NotFoundError,
    OutsideBusinessHoursError,
    ServiceNotFoundError,
    SlotInThePastError,
    SlotUnavailableError,
)
from booking_agent.domain.ports import Clock, DatabaseHealthChecker, IntentParser

__all__ = [
    "AppointmentInThePastError",
    "AppointmentNotConfirmedError",
    "AppointmentNotFoundError",
    "AppointmentStatus",
    "BarberNotFoundError",
    "BookingIntent",
    "Clock",
    "ConversationStateNotFoundError",
    "CustomerNotFoundError",
    "DatabaseHealthChecker",
    "DomainError",
    "IntentParser",
    "IntentType",
    "InvalidLocalTimeError",
    "InvalidTimeWindowError",
    "MessageStatus",
    "MinimumNoticeViolationError",
    "NotAppointmentOwnerError",
    "NotFoundError",
    "OutsideBusinessHoursError",
    "ServiceNotFoundError",
    "SlotInThePastError",
    "SlotUnavailableError",
]
