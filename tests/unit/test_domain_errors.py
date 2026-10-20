"""Unit tests for domain error classes and Clock port implementations."""

from datetime import UTC, datetime

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
    LLMConfigurationError,
    LLMError,
    LLMInvalidOutputError,
    LLMUnavailableError,
    MinimumNoticeViolationError,
    NotAppointmentOwnerError,
    NotFoundError,
    OutsideBusinessHoursError,
    ServiceNotFoundError,
    SlotInThePastError,
    SlotUnavailableError,
)
from booking_agent.domain.ports import Clock
from booking_agent.integrations.system_clock import SystemClock
from tests.fakes.fake_clock import FakeClock


def test_domain_error_inheritance() -> None:
    assert issubclass(LLMError, DomainError)
    assert issubclass(LLMConfigurationError, LLMError)
    assert issubclass(LLMUnavailableError, LLMError)
    assert issubclass(LLMInvalidOutputError, LLMError)
    assert issubclass(SlotUnavailableError, DomainError)
    assert issubclass(OutsideBusinessHoursError, DomainError)
    assert issubclass(SlotInThePastError, DomainError)
    assert issubclass(MinimumNoticeViolationError, DomainError)
    assert issubclass(InvalidTimeWindowError, DomainError)
    assert issubclass(InvalidLocalTimeError, DomainError)
    assert issubclass(NotAppointmentOwnerError, DomainError)
    assert issubclass(AppointmentNotConfirmedError, DomainError)
    assert issubclass(AppointmentInThePastError, DomainError)
    assert issubclass(NotFoundError, DomainError)
    assert issubclass(CustomerNotFoundError, NotFoundError)
    assert issubclass(ServiceNotFoundError, NotFoundError)
    assert issubclass(BarberNotFoundError, NotFoundError)
    assert issubclass(AppointmentNotFoundError, NotFoundError)
    assert issubclass(ConversationStateNotFoundError, NotFoundError)


def test_slot_unavailable_error_defaults_and_alternatives() -> None:
    err_without_alts = SlotUnavailableError("Slot taken")
    assert str(err_without_alts) == "Slot taken"
    assert err_without_alts.alternatives == []

    alt1 = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    alt2 = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
    err_with_alts = SlotUnavailableError("Slot taken", alternatives=[alt1, alt2])
    assert err_with_alts.alternatives == [alt1, alt2]


def test_system_clock_protocol_conformance() -> None:
    clock: Clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None
    assert now.tzinfo == UTC


def test_fake_clock_time_manipulation() -> None:
    fixed_time = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    clock = FakeClock(fixed_time)
    assert clock.now() == fixed_time

    new_time = datetime(2026, 10, 19, 11, 30, tzinfo=UTC)
    clock.set_time(new_time)
    assert clock.now() == new_time
