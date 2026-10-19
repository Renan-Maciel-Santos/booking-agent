"""Domain-level error hierarchy for business logic and repository errors."""

from datetime import datetime


class DomainError(Exception):
    """Base class for all domain errors."""


class SlotUnavailableError(DomainError):
    """Raised when an appointment slot is unavailable due to an overlapping booking."""

    def __init__(self, message: str, alternatives: list[datetime] | None = None) -> None:
        """Initialize with an error message and optional alternative available slot datetimes."""
        super().__init__(message)
        self.alternatives: list[datetime] = alternatives if alternatives is not None else []


class OutsideBusinessHoursError(DomainError):
    """Raised when an appointment time falls outside salon business hours."""


class SlotInThePastError(DomainError):
    """Raised when an appointment is requested for a past time."""


class MinimumNoticeViolationError(DomainError):
    """Raised when an appointment is booked without sufficient advance notice."""


class InvalidTimeWindowError(DomainError):
    """Raised when a time window is invalid or exceeds the maximum horizon."""


class InvalidLocalTimeError(DomainError):
    """Raised when local wall-clock time does not exist due to daylight saving transitions."""


class NotAppointmentOwnerError(DomainError):
    """Raised when an operation is attempted on an appointment owned by another customer."""


class AppointmentNotConfirmedError(DomainError):
    """Raised when attempting an operation on an appointment that is not confirmed."""


class AppointmentInThePastError(DomainError):
    """Raised when attempting to modify or cancel an appointment that has already passed."""


class NotFoundError(DomainError):
    """Base class for errors when an entity is not found."""


class CustomerNotFoundError(NotFoundError):
    """Raised when a customer cannot be found."""


class ServiceNotFoundError(NotFoundError):
    """Raised when a service cannot be found."""


class BarberNotFoundError(NotFoundError):
    """Raised when a barber cannot be found."""


class AppointmentNotFoundError(NotFoundError):
    """Raised when an appointment cannot be found."""


class ConversationStateNotFoundError(NotFoundError):
    """Raised when a conversation state cannot be found."""
