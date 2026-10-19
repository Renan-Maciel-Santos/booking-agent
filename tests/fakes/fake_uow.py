"""In-memory fake implementation of UnitOfWork and repositories for unit testing."""

import uuid
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from types import TracebackType
from typing import Self

from booking_agent.database.models import Appointment, Barber, BusinessHours, Customer, Service
from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.errors import SlotUnavailableError
from booking_agent.domain.ports import (
    AppointmentRepositoryPort,
    BarberRepositoryPort,
    BusinessHoursRepositoryPort,
    CustomerRepositoryPort,
    ServiceRepositoryPort,
    UnitOfWork,
)


class InMemoryDataStore:
    """State storage shared across UnitOfWork instances."""

    def __init__(self) -> None:
        """Initialize empty entity dictionaries."""
        self.appointments: dict[uuid.UUID, Appointment] = {}
        self.barbers: dict[uuid.UUID, Barber] = {}
        self.business_hours: dict[int, BusinessHours] = {}
        self.customers: dict[uuid.UUID, Customer] = {}
        self.services: dict[uuid.UUID, Service] = {}


class FakeAppointmentRepository:
    """Fake repository managing Appointment persistence in memory."""

    def __init__(
        self, store: InMemoryDataStore, staged_appointments: dict[uuid.UUID, Appointment]
    ) -> None:
        """Store reference to shared store and staged mutations."""
        self._store = store
        self._staged = staged_appointments

    async def list_by_barber_and_window(
        self,
        barber_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[Appointment]:
        """Return confirmed appointments for a barber overlapping the window."""
        results: list[Appointment] = []
        for appt in self._staged.values():
            if (
                appt.barber_id == barber_id
                and appt.status == AppointmentStatus.CONFIRMED.value
                and appt.starts_at < window_end
                and appt.ends_at > window_start
            ):
                results.append(appt)
        results.sort(key=lambda a: a.starts_at)
        return results

    async def get_by_id(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch appointment by primary key."""
        return self._staged.get(appointment_id)

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
        """Create appointment, raising SlotUnavailableError if overlapping confirmed booking."""
        for appt in self._staged.values():
            if (
                appt.barber_id == barber_id
                and appt.status == AppointmentStatus.CONFIRMED.value
                and starts_at < appt.ends_at
                and ends_at > appt.starts_at
            ):
                slot_str = f"[{starts_at.isoformat()}, {ends_at.isoformat()})"
                raise SlotUnavailableError(f"Slot {slot_str} is unavailable.")

        appt_id = appointment_id or uuid.uuid4()
        appointment = Appointment(
            id=appt_id,
            customer_id=customer_id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=price_cents,
            status=status.value,
            created_at=starts_at,
            updated_at=starts_at,
        )
        self._staged[appt_id] = appointment
        return appointment

    async def reschedule(
        self,
        appointment_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
    ) -> Appointment | None:
        """Update appointment boundaries in-place, raising SlotUnavailableError on collision."""
        appt = self._staged.get(appointment_id)
        if appt is None:
            return None

        for other_id, other in self._staged.items():
            if (
                other_id != appointment_id
                and other.barber_id == appt.barber_id
                and other.status == AppointmentStatus.CONFIRMED.value
                and starts_at < other.ends_at
                and ends_at > other.starts_at
            ):
                slot_str = f"[{starts_at.isoformat()}, {ends_at.isoformat()})"
                raise SlotUnavailableError(f"Slot {slot_str} is unavailable.")

        appt.starts_at = starts_at
        appt.ends_at = ends_at
        return appt

    async def cancel(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Mark appointment as cancelled."""
        appt = self._staged.get(appointment_id)
        if appt is None:
            return None
        appt.status = AppointmentStatus.CANCELLED.value
        return appt


class FakeBarberRepository:
    """Fake repository managing Barber persistence in memory."""

    def __init__(self, store: InMemoryDataStore) -> None:
        """Store reference to shared store."""
        self._store = store

    async def get_by_id(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch barber by ID."""
        return self._store.barbers.get(barber_id)

    async def list_active(self) -> Sequence[Barber]:
        """List active barbers ordered by name, id."""
        active = [b for b in self._store.barbers.values() if b.active]
        active.sort(key=lambda b: (b.name, str(b.id)))
        return active


class FakeBusinessHoursRepository:
    """Fake repository managing BusinessHours schedule in memory."""

    def __init__(self, store: InMemoryDataStore) -> None:
        """Store reference to shared store."""
        self._store = store

    async def get_by_day(self, day_of_week: int) -> BusinessHours | None:
        """Fetch operating hours for weekday."""
        return self._store.business_hours.get(day_of_week)

    async def list_all(self) -> Sequence[BusinessHours]:
        """Fetch all configured hours."""
        hours = list(self._store.business_hours.values())
        hours.sort(key=lambda h: h.day_of_week)
        return hours


class FakeCustomerRepository:
    """Fake repository managing Customer records in memory."""

    def __init__(
        self, store: InMemoryDataStore, staged_customers: dict[uuid.UUID, Customer]
    ) -> None:
        """Store reference to shared store and staged customers."""
        self._store = store
        self._staged = staged_customers

    async def get_by_id(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch customer by ID."""
        return self._staged.get(customer_id)

    async def get_by_phone(self, phone: str) -> Customer | None:
        """Fetch customer by phone."""
        for c in self._staged.values():
            if c.phone == phone:
                return c
        return None

    async def create(
        self,
        name: str,
        phone: str,
        customer_id: uuid.UUID | None = None,
    ) -> Customer:
        """Create new customer record."""
        cust_id = customer_id or uuid.uuid4()
        customer = Customer(id=cust_id, name=name, phone=phone)
        self._staged[cust_id] = customer
        return customer

    async def update_name(self, customer_id: uuid.UUID, name: str) -> Customer | None:
        """Update customer name."""
        customer = self._staged.get(customer_id)
        if customer is None:
            return None
        customer.name = name
        return customer


class FakeServiceRepository:
    """Fake repository managing Service catalog in memory."""

    def __init__(self, store: InMemoryDataStore) -> None:
        """Store reference to shared store."""
        self._store = store

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        """Fetch service by ID."""
        return self._store.services.get(service_id)

    async def list_active(self) -> Sequence[Service]:
        """List active services."""
        services = [s for s in self._store.services.values() if s.active]
        services.sort(key=lambda s: s.name)
        return services


class FakeUnitOfWork:
    """Unit of work managing transactions over in-memory repositories."""

    def __init__(self, store: InMemoryDataStore) -> None:
        """Initialize with parent store and stage local copies."""
        self._store = store
        self._staged_appointments = deepcopy(store.appointments)
        self._staged_customers = deepcopy(store.customers)

        self._appointments = FakeAppointmentRepository(store, self._staged_appointments)
        self._barbers = FakeBarberRepository(store)
        self._business_hours = FakeBusinessHoursRepository(store)
        self._customers = FakeCustomerRepository(store, self._staged_customers)
        self._services = FakeServiceRepository(store)

    @property
    def appointments(self) -> AppointmentRepositoryPort:
        """Active appointment repository."""
        return self._appointments

    @property
    def barbers(self) -> BarberRepositoryPort:
        """Active barber repository."""
        return self._barbers

    @property
    def business_hours(self) -> BusinessHoursRepositoryPort:
        """Active business hours repository."""
        return self._business_hours

    @property
    def customers(self) -> CustomerRepositoryPort:
        """Active customer repository."""
        return self._customers

    @property
    def services(self) -> ServiceRepositoryPort:
        """Active service repository."""
        return self._services

    async def __aenter__(self) -> Self:
        """Enter transaction context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Commit staged changes to shared store on clean exit; discard on exception."""
        if exc_type is None:
            self._store.appointments = self._staged_appointments
            self._store.customers = self._staged_customers


class FakeUnitOfWorkFactory:
    """Factory producing FakeUnitOfWork instances sharing a data store."""

    def __init__(self, store: InMemoryDataStore | None = None) -> None:
        """Initialize with optional existing store or create a new one."""
        self.store = store or InMemoryDataStore()

    def __call__(self) -> UnitOfWork:
        """Create and return a new FakeUnitOfWork."""
        return FakeUnitOfWork(self.store)
