"""Domain-level error hierarchy for business logic and repository errors."""


class DomainError(Exception):
    """Base class for all domain errors."""


class SlotUnavailableError(DomainError):
    """Raised when an appointment slot is unavailable due to an overlapping booking."""


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
