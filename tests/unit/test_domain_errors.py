"""Unit tests for domain error classes."""

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


def test_domain_error_inheritance() -> None:
    assert issubclass(SlotUnavailableError, DomainError)
    assert issubclass(NotFoundError, DomainError)
    assert issubclass(CustomerNotFoundError, NotFoundError)
    assert issubclass(ServiceNotFoundError, NotFoundError)
    assert issubclass(BarberNotFoundError, NotFoundError)
    assert issubclass(AppointmentNotFoundError, NotFoundError)
    assert issubclass(ConversationStateNotFoundError, NotFoundError)


def test_domain_error_message() -> None:
    error = SlotUnavailableError("Barber already booked for this slot")
    assert str(error) == "Barber already booked for this slot"
