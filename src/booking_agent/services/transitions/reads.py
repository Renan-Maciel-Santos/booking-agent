"""Read-only data access interface and UnitOfWork adapter for state transitions."""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Protocol

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.models import AvailabilityResult
from booking_agent.domain.ports import UnitOfWork
from booking_agent.services.booking_service import BookingService


class ConversationReads(Protocol):
    """Read-only interface providing domain queries needed by stage transitions.

    Guarantees by type that state transition handlers cannot mutate state,
    execute actions, or persist data.
    """

    async def get_appointment(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch an appointment by its unique identifier."""
        ...

    async def list_customer_appointments(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """List all appointments belonging to a given customer."""
        ...

    async def get_customer(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch a customer by their unique identifier."""
        ...

    async def get_customer_by_phone(self, phone: str) -> Customer | None:
        """Fetch a customer by their normalized phone number."""
        ...

    async def get_service(self, service_id: uuid.UUID) -> Service | None:
        """Fetch a service by its unique identifier."""
        ...

    async def list_active_services(self) -> Sequence[Service]:
        """List all currently active services offered by the business."""
        ...

    async def get_barber(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch a barber by their unique identifier."""
        ...

    async def list_active_barbers(self) -> Sequence[Barber]:
        """List all currently active barbers employed by the business."""
        ...

    async def list_availability(
        self,
        service_id: uuid.UUID,
        target_date: date,
        barber_id: uuid.UUID | None = None,
    ) -> AvailabilityResult:
        """List available booking slots for a given service, date, and optional barber."""
        ...


class UnitOfWorkReads:
    """Adapter implementing ConversationReads using an already open UnitOfWork.

    This adapter operates strictly within an existing transaction under lock
    and delegates directly to the underlying repositories and BookingService.
    It never creates or opens a new unit of work.
    """

    def __init__(self, uow: UnitOfWork, booking_service: BookingService) -> None:
        """Initialize with an already-opened UnitOfWork and BookingService.

        Args:
            uow: Active UnitOfWork transaction under conversation lock.
            booking_service: BookingService instance for availability calculations.
        """
        self._uow = uow
        self._booking_service = booking_service

    async def get_appointment(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch an appointment by its unique identifier."""
        return await self._uow.appointments.get_by_id(appointment_id)

    async def list_customer_appointments(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """List all appointments belonging to a given customer."""
        return await self._uow.appointments.list_by_customer(customer_id)

    async def get_customer(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch a customer by their unique identifier."""
        return await self._uow.customers.get_by_id(customer_id)

    async def get_customer_by_phone(self, phone: str) -> Customer | None:
        """Fetch a customer by their normalized phone number."""
        return await self._uow.customers.get_by_phone(phone)

    async def get_service(self, service_id: uuid.UUID) -> Service | None:
        """Fetch a service by its unique identifier."""
        return await self._uow.services.get_by_id(service_id)

    async def list_active_services(self) -> Sequence[Service]:
        """List all currently active services offered by the business."""
        return await self._uow.services.list_active()

    async def get_barber(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch a barber by their unique identifier."""
        return await self._uow.barbers.get_by_id(barber_id)

    async def list_active_barbers(self) -> Sequence[Barber]:
        """List all currently active barbers employed by the business."""
        return await self._uow.barbers.list_active()

    async def list_availability(
        self,
        service_id: uuid.UUID,
        target_date: date,
        barber_id: uuid.UUID | None = None,
    ) -> AvailabilityResult:
        """List available booking slots for a given service, date, and optional barber."""
        return await self._booking_service.list_availability(
            service_id=service_id,
            target_date=target_date,
            barber_id=barber_id,
        )
