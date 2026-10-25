"""Ports: contracts the inner layers need, implemented by outer layers.

Services depend on these Protocols, never on concrete adapters or repositories,
so dependencies always point inwards (Dependency Inversion Principle).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from booking_agent.database.models import (
    Appointment,
    Barber,
    BookingAction,
    BusinessHours,
    Customer,
    ProcessedMessage,
    Service,
)
from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ConversationSlots, ConversationSnapshot
from booking_agent.domain.enums import AppointmentStatus, ConversationStage
from booking_agent.domain.models import MessageAcquisition


class IntentParser(Protocol):
    """Turns a raw user message into a validated BookingIntent.

    Implementations (e.g. an OpenAI-backed adapter) live in
    booking_agent.integrations.llm.
    """

    async def parse(self, user_message: str) -> BookingIntent:
        """Extract intent and entities from a user message.

        Args:
            user_message: Raw text sent by the end user.

        Returns:
            Validated booking intent.
        """
        ...


class DatabaseHealthChecker(Protocol):
    """Verifies that the database can be reached.

    Implemented by booking_agent.repositories.health_repository.HealthRepository.
    """

    async def ping(self) -> None:
        """Raise if the database cannot be reached."""
        ...


class Clock(Protocol):
    """Port for time-dependent operations.

    Implemented by booking_agent.integrations.system_clock.SystemClock in production,
    and by test doubles (e.g. FakeClock) in tests.
    """

    def now(self) -> datetime:
        """Return the current timezone-aware datetime in UTC."""
        ...


class AppointmentRepositoryPort(Protocol):
    """Port for Appointment persistence and slot exclusivity checks."""

    async def list_by_barber_and_window(
        self,
        barber_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[Appointment]:
        """Fetch confirmed appointments for a barber overlapping a time window."""
        ...

    async def get_by_id(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch an appointment by primary key."""
        ...

    async def list_by_customer(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """Fetch all appointments for a given customer ordered by starts_at."""
        ...

    async def create(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        barber_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        price_cents: int,
        status: AppointmentStatus = AppointmentStatus.CONFIRMED,
        appointment_id: uuid.UUID | None = None,
    ) -> Appointment:
        """Create and flush a new appointment."""
        ...

    async def reschedule(
        self,
        appointment_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
    ) -> Appointment | None:
        """Update appointment boundaries in-place."""
        ...

    async def cancel(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Mark an appointment as cancelled."""
        ...


class BarberRepositoryPort(Protocol):
    """Port for Barber resource operations."""

    async def get_by_id(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch a barber by primary key."""
        ...

    async def list_active(self) -> Sequence[Barber]:
        """Return all active barbers ordered by name."""
        ...


class BusinessHoursRepositoryPort(Protocol):
    """Port for business hours schedule queries."""

    async def get_by_day(self, day_of_week: int) -> BusinessHours | None:
        """Fetch operating hours for a specific weekday."""
        ...

    async def list_all(self) -> Sequence[BusinessHours]:
        """Fetch all configured business hours ordered by day_of_week."""
        ...


class CustomerRepositoryPort(Protocol):
    """Port for Customer database operations."""

    async def get_by_id(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch a customer by primary key."""
        ...

    async def get_by_phone(self, phone: str) -> Customer | None:
        """Fetch a customer by unique phone number."""
        ...

    async def create(
        self,
        name: str,
        phone: str,
        customer_id: uuid.UUID | None = None,
    ) -> Customer:
        """Create and flush a new customer record."""
        ...

    async def update_name(self, customer_id: uuid.UUID, name: str) -> Customer | None:
        """Update customer name."""
        ...


class ServiceRepositoryPort(Protocol):
    """Port for Service catalog queries."""

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        """Fetch a service by primary key."""
        ...

    async def list_active(self) -> Sequence[Service]:
        """Return all active services ordered by name."""
        ...


class BookingActionRepositoryPort(Protocol):
    """Port for BookingAction persistence and idempotency tracking."""

    async def get_by_id(self, action_id: uuid.UUID) -> BookingAction | None:
        """Fetch a booking action by primary key."""
        ...

    async def record_action(
        self,
        action_id: uuid.UUID,
        kind: str,
        appointment_id: uuid.UUID,
        result: dict[str, Any],
    ) -> BookingAction:
        """Record an executed booking mutation."""
        ...


class ConversationStateRepositoryPort(Protocol):
    """Port for ConversationState persistence and locking."""

    async def get(self, phone: str, for_update: bool = False) -> ConversationSnapshot | None:
        """Fetch dialogue state for a phone number.

        Raises:
            InvalidConversationStateError: If the persisted JSON fails domain validation.
        """
        ...

    async def upsert(
        self,
        phone: str,
        stage: ConversationStage,
        slots: ConversationSlots,
    ) -> ConversationSnapshot:
        """Insert or update conversation state."""
        ...


class ProcessedMessageRepositoryPort(Protocol):
    """Port for ProcessedMessage idempotency tracking and leasing."""

    async def get(self, message_id: str) -> ProcessedMessage | None:
        """Fetch a processed message record by primary key."""
        ...

    async def acquire(self, message_id: str, timeout_seconds: int = 300) -> MessageAcquisition:
        """Atomically acquire processing rights for a message ID."""
        ...

    async def mark_done(self, message_id: str, response_text: str | None = None) -> bool:
        """Mark a message as done with optional response_text."""
        ...

    async def mark_failed(self, message_id: str) -> bool:
        """Mark a message as failed."""
        ...


class UnitOfWork(Protocol):
    """Unit of work protocol managing transaction boundary and repository access."""

    @property
    def appointments(self) -> AppointmentRepositoryPort:
        """Appointment repository."""
        ...

    @property
    def barbers(self) -> BarberRepositoryPort:
        """Barber repository."""
        ...

    @property
    def booking_actions(self) -> BookingActionRepositoryPort:
        """Booking actions repository."""
        ...

    @property
    def business_hours(self) -> BusinessHoursRepositoryPort:
        """Business hours repository."""
        ...

    @property
    def conversation_states(self) -> ConversationStateRepositoryPort:
        """Conversation state repository."""
        ...

    @property
    def customers(self) -> CustomerRepositoryPort:
        """Customer repository."""
        ...

    @property
    def processed_messages(self) -> ProcessedMessageRepositoryPort:
        """Processed message repository."""
        ...

    @property
    def services(self) -> ServiceRepositoryPort:
        """Service repository."""
        ...

    async def __aenter__(self) -> Self:
        """Enter transaction context."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit transaction context, committing on success or rolling back on error."""
        ...


class UnitOfWorkFactory(Protocol):
    """Factory creating UnitOfWork context manager instances."""

    def __call__(self) -> UnitOfWork:
        """Return a new UnitOfWork instance."""
        ...
