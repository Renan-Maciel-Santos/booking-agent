"""In-memory implementation of ConversationReads for fast, isolated tests."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime

from booking_agent.database.models import Appointment, Barber, Customer, Service
from booking_agent.domain.models import AvailabilityResult
from booking_agent.services.transitions.reads import ConversationReads


class InMemoryReads(ConversationReads):
    """In-memory implementation of ConversationReads protocol."""

    def __init__(
        self,
        appointments: dict[uuid.UUID, Appointment] | None = None,
        customers: dict[uuid.UUID, Customer] | None = None,
        services: dict[uuid.UUID, Service] | None = None,
        barbers: dict[uuid.UUID, Barber] | None = None,
        availability_slots: Sequence[datetime] | None = None,
    ) -> None:
        """Initialize in-memory read storage.

        Args:
            appointments: Mapping of appointment ID to Appointment.
            customers: Mapping of customer ID to Customer.
            services: Mapping of service ID to Service.
            barbers: Mapping of barber ID to Barber.
            availability_slots: Default sequence of available datetimes.
        """
        self.appointments = dict(appointments or {})
        self.customers = dict(customers or {})
        self.services = dict(services or {})
        self.barbers = dict(barbers or {})
        self.availability_slots = list(availability_slots or [])

    async def get_appointment(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch appointment by ID."""
        return self.appointments.get(appointment_id)

    async def list_customer_appointments(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """Fetch all appointments for a given customer."""
        return [a for a in self.appointments.values() if a.customer_id == customer_id]

    async def get_customer(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch customer by primary ID."""
        return self.customers.get(customer_id)

    async def get_customer_by_phone(self, phone: str) -> Customer | None:
        """Fetch customer by phone number."""
        for c in self.customers.values():
            if c.phone == phone:
                return c
        return None

    async def get_service(self, service_id: uuid.UUID) -> Service | None:
        """Fetch service by ID."""
        return self.services.get(service_id)

    async def list_active_services(self) -> Sequence[Service]:
        """List all active services."""
        return [s for s in self.services.values() if s.active]

    async def get_barber(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch barber by ID."""
        return self.barbers.get(barber_id)

    async def list_active_barbers(self) -> Sequence[Barber]:
        """List all active barbers."""
        return [b for b in self.barbers.values() if b.active]

    async def list_availability(
        self,
        service_id: uuid.UUID,
        target_date: date,
        barber_id: uuid.UUID | None = None,
    ) -> AvailabilityResult:
        """Query availability for a service, date and optional barber."""
        return AvailabilityResult(
            service_id=service_id,
            target_date=target_date,
            slots=self.availability_slots,
            barber_id=barber_id,
        )
