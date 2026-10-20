"""SQLAlchemy implementation of domain.ports.UnitOfWork."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.domain.ports import UnitOfWork
from booking_agent.repositories.appointment_repository import AppointmentRepository
from booking_agent.repositories.barber_repository import BarberRepository
from booking_agent.repositories.booking_action_repository import BookingActionRepository
from booking_agent.repositories.business_hours_repository import BusinessHoursRepository
from booking_agent.repositories.conversation_state_repository import ConversationStateRepository
from booking_agent.repositories.customer_repository import CustomerRepository
from booking_agent.repositories.processed_message_repository import ProcessedMessageRepository
from booking_agent.repositories.service_repository import ServiceRepository


class SqlAlchemyUnitOfWork:
    """Unit of work managing SQLAlchemy session transactions and repositories."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Store the injected session factory.

        Args:
            session_factory: Factory producing AsyncSession instances.
        """
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._appointments: AppointmentRepository | None = None
        self._barbers: BarberRepository | None = None
        self._booking_actions: BookingActionRepository | None = None
        self._business_hours: BusinessHoursRepository | None = None
        self._conversation_states: ConversationStateRepository | None = None
        self._customers: CustomerRepository | None = None
        self._processed_messages: ProcessedMessageRepository | None = None
        self._services: ServiceRepository | None = None

    @property
    def appointments(self) -> AppointmentRepository:
        """Active appointment repository."""
        if self._appointments is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._appointments

    @property
    def barbers(self) -> BarberRepository:
        """Active barber repository."""
        if self._barbers is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._barbers

    @property
    def booking_actions(self) -> BookingActionRepository:
        """Active booking action repository."""
        if self._booking_actions is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._booking_actions

    @property
    def business_hours(self) -> BusinessHoursRepository:
        """Active business hours repository."""
        if self._business_hours is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._business_hours

    @property
    def conversation_states(self) -> ConversationStateRepository:
        """Active conversation state repository."""
        if self._conversation_states is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._conversation_states

    @property
    def customers(self) -> CustomerRepository:
        """Active customer repository."""
        if self._customers is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._customers

    @property
    def processed_messages(self) -> ProcessedMessageRepository:
        """Active processed message repository."""
        if self._processed_messages is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._processed_messages

    @property
    def services(self) -> ServiceRepository:
        """Active service repository."""
        if self._services is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._services

    async def __aenter__(self) -> Self:
        """Create a new session, instantiate repositories, and start transaction.

        Returns:
            The active UnitOfWork context.
        """
        self._session = self._session_factory()
        self._appointments = AppointmentRepository(self._session)
        self._barbers = BarberRepository(self._session)
        self._booking_actions = BookingActionRepository(self._session)
        self._business_hours = BusinessHoursRepository(self._session)
        self._conversation_states = ConversationStateRepository(self._session)
        self._customers = CustomerRepository(self._session)
        self._processed_messages = ProcessedMessageRepository(self._session)
        self._services = ServiceRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Roll back on exception or commit on clean exit, then close session.

        Args:
            exc_type: Exception type if raised, or None.
            exc_val: Exception value if raised, or None.
            exc_tb: Exception traceback if raised, or None.
        """
        if self._session is not None:
            try:
                if exc_type is not None:
                    await self._session.rollback()
                else:
                    await self._session.commit()
            finally:
                await self._session.close()


class SqlAlchemyUnitOfWorkFactory:
    """Callable factory producing UnitOfWork instances."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Store the session factory.

        Args:
            session_factory: Bound async sessionmaker.
        """
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        """Create and return a new UnitOfWork.

        Returns:
            Configured SqlAlchemyUnitOfWork instance conforming to UnitOfWork.
        """
        return SqlAlchemyUnitOfWork(self._session_factory)
