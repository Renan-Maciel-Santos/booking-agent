import uuid
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from booking_agent.database.models import (
    Appointment,
    Barber,
    BookingAction,
    BusinessHours,
    ConversationState,
    Customer,
    ProcessedMessage,
    Service,
)
from booking_agent.domain.conversation_state import (
    ConversationSlots,
    ConversationSnapshot,
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import AppointmentStatus, ConversationStage, MessageStatus
from booking_agent.domain.errors import (
    ActionAlreadyExistsError,
    InvalidConversationStateError,
    SlotUnavailableError,
)
from booking_agent.domain.models import MessageAcquisition
from booking_agent.domain.ports import (
    AppointmentRepositoryPort,
    BarberRepositoryPort,
    BookingActionRepositoryPort,
    BusinessHoursRepositoryPort,
    Clock,
    ConversationStateRepositoryPort,
    CustomerRepositoryPort,
    ProcessedMessageRepositoryPort,
    ServiceRepositoryPort,
    UnitOfWork,
)


class InMemoryDataStore:
    """State storage shared across UnitOfWork instances."""

    def __init__(self) -> None:
        """Initialize empty entity dictionaries."""
        self.appointments: dict[uuid.UUID, Appointment] = {}
        self.barbers: dict[uuid.UUID, Barber] = {}
        self.booking_actions: dict[uuid.UUID, BookingAction] = {}
        self.business_hours: dict[int, BusinessHours] = {}
        self.conversation_states: dict[str, ConversationState] = {}
        self.customers: dict[uuid.UUID, Customer] = {}
        self.processed_messages: dict[str, ProcessedMessage] = {}
        self.services: dict[uuid.UUID, Service] = {}

    def seed_conversation_state(
        self,
        phone: str,
        stage: str,
        slots: dict[str, Any],
        updated_at: datetime | None = None,
    ) -> None:
        """Seed raw unvalidated conversation state dictionary directly in store."""
        now = updated_at or datetime.now(UTC)
        self.conversation_states[phone] = ConversationState(
            phone=phone,
            stage=stage,
            slots=deepcopy(slots),
            created_at=now,
            updated_at=now,
        )


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

    async def list_by_customer(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """Fetch all appointments for a given customer ordered by starts_at."""
        results = [a for a in self._staged.values() if a.customer_id == customer_id]
        results.sort(key=lambda a: a.starts_at)
        return results

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


class FakeBookingActionRepository:
    """Fake repository managing BookingAction persistence in memory."""

    def __init__(
        self,
        store: InMemoryDataStore,
        staged_actions: dict[uuid.UUID, BookingAction],
    ) -> None:
        """Store reference to shared store and staged actions."""
        self._store = store
        self._staged = staged_actions

    async def get_by_id(self, action_id: uuid.UUID) -> BookingAction | None:
        """Fetch booking action by primary key."""
        return self._staged.get(action_id)

    async def record_action(
        self,
        action_id: uuid.UUID,
        kind: str,
        appointment_id: uuid.UUID,
        result: dict[str, Any],
    ) -> BookingAction:
        """Record an executed booking mutation."""
        if action_id in self._staged:
            raise ActionAlreadyExistsError(str(action_id))
        action = BookingAction(
            action_id=action_id,
            kind=kind,
            appointment_id=appointment_id,
            result=result,
            created_at=datetime.now(UTC),
        )
        self._staged[action_id] = action
        return action


class FakeConversationStateRepository:
    """Fake repository managing ConversationState persistence in memory."""

    def __init__(
        self,
        store: InMemoryDataStore,
        staged_states: dict[str, ConversationState],
        clock: Clock | None = None,
    ) -> None:
        """Store reference to shared store and staged states."""
        self._store = store
        self._staged = staged_states
        self._clock = clock

    def seed_raw(
        self,
        phone: str,
        stage: str,
        slots: dict[str, Any],
        updated_at: datetime | None = None,
    ) -> None:
        """Seed raw unvalidated conversation state dictionary directly in staged state."""
        now = updated_at or (self._clock.now() if self._clock is not None else datetime.now(UTC))
        state = ConversationState(
            phone=phone,
            stage=stage,
            slots=deepcopy(slots),
            created_at=now,
            updated_at=now,
        )
        self._staged[phone] = state

    async def get(self, phone: str, for_update: bool = False) -> ConversationSnapshot | None:
        """Fetch dialogue state for a phone number."""
        state = self._staged.get(phone)
        if state is None:
            return None

        invalid_fields: list[str] = []
        parsed_stage: ConversationStage | None = None
        try:
            parsed_stage = ConversationStage(state.stage)
        except ValueError:
            invalid_fields.append("stage")

        try:
            slots = slots_from_json(state.slots)
        except ValidationError as exc:
            raise InvalidConversationStateError.from_validation_error(
                exc, extra_fields=tuple(invalid_fields)
            ) from exc

        if invalid_fields or parsed_stage is None:
            raise InvalidConversationStateError(tuple(invalid_fields))

        return ConversationSnapshot(
            stage=parsed_stage,
            slots=slots,
            updated_at=state.updated_at,
        )

    async def upsert(
        self,
        phone: str,
        stage: ConversationStage,
        slots: ConversationSlots,
    ) -> ConversationSnapshot:
        """Insert or update conversation state."""
        raw_slots = slots_to_json(slots)
        state = self._staged.get(phone)
        now = self._clock.now() if self._clock is not None else datetime.now(UTC)
        if state is None:
            state = ConversationState(
                phone=phone,
                stage=stage.value,
                slots=raw_slots,
                created_at=now,
                updated_at=now,
            )
            self._staged[phone] = state
        else:
            state.stage = stage.value
            state.slots = raw_slots
            state.updated_at = now
        return ConversationSnapshot(
            stage=stage,
            slots=slots,
            updated_at=now,
        )


class FakeProcessedMessageRepository:
    """Fake repository managing ProcessedMessage persistence and leasing."""

    def __init__(
        self,
        store: InMemoryDataStore,
        staged_messages: dict[str, ProcessedMessage],
        clock: Clock | None = None,
    ) -> None:
        """Store reference to shared store and staged messages."""
        self._store = store
        self._staged = staged_messages
        self._clock = clock

    async def get(self, message_id: str) -> ProcessedMessage | None:
        """Fetch a processed message record by primary key."""
        msg = self._staged.get(message_id)
        return deepcopy(msg) if msg is not None else None

    async def acquire(self, message_id: str, timeout_seconds: int = 300) -> MessageAcquisition:
        """Atomically acquire processing rights for a message ID."""
        now = self._clock.now() if self._clock is not None else datetime.now(UTC)
        existing = self._staged.get(message_id)
        if existing is None:
            new_msg = ProcessedMessage(
                message_id=message_id,
                status=MessageStatus.PROCESSING.value,
                response_text=None,
                created_at=now,
                updated_at=now,
            )
            self._staged[message_id] = new_msg
            return MessageAcquisition(acquired=True, status=MessageStatus.PROCESSING)

        if existing.status == MessageStatus.DONE.value:
            return MessageAcquisition(
                acquired=False,
                status=MessageStatus.DONE,
                response_text=existing.response_text,
            )

        if existing.status == MessageStatus.PROCESSING.value:
            elapsed = (now - existing.updated_at).total_seconds()
            if elapsed > timeout_seconds:
                existing.updated_at = now
                return MessageAcquisition(acquired=True, status=MessageStatus.PROCESSING)
            return MessageAcquisition(acquired=False, status=MessageStatus.PROCESSING)

        existing.status = MessageStatus.PROCESSING.value
        existing.updated_at = now
        return MessageAcquisition(acquired=True, status=MessageStatus.PROCESSING)

    async def mark_done(self, message_id: str, response_text: str | None = None) -> bool:
        """Mark a message as done with optional response_text."""
        existing = self._staged.get(message_id)
        if existing is not None:
            now = self._clock.now() if self._clock is not None else datetime.now(UTC)
            existing.status = MessageStatus.DONE.value
            existing.response_text = response_text
            existing.updated_at = now
            return True
        return False

    async def mark_failed(self, message_id: str) -> bool:
        """Mark a message as failed."""
        existing = self._staged.get(message_id)
        if existing is not None:
            now = self._clock.now() if self._clock is not None else datetime.now(UTC)
            existing.status = MessageStatus.FAILED.value
            existing.updated_at = now
            return True
        return False


class FakeUnitOfWork:
    """Unit of work managing transactions over in-memory repositories."""

    def __init__(
        self,
        store: InMemoryDataStore,
        clock: Clock | None = None,
    ) -> None:
        """Initialize with parent store and stage local copies."""
        self._store = store
        self._clock = clock
        self._staged_appointments = deepcopy(store.appointments)
        self._staged_booking_actions = deepcopy(store.booking_actions)
        self._staged_conversation_states = deepcopy(store.conversation_states)
        self._staged_customers = deepcopy(store.customers)
        self._staged_processed_messages = deepcopy(store.processed_messages)

        self._appointments = FakeAppointmentRepository(store, self._staged_appointments)
        self._barbers = FakeBarberRepository(store)
        self._booking_actions = FakeBookingActionRepository(store, self._staged_booking_actions)
        self._business_hours = FakeBusinessHoursRepository(store)
        self._conversation_states = FakeConversationStateRepository(
            store, self._staged_conversation_states, clock=clock
        )
        self._customers = FakeCustomerRepository(store, self._staged_customers)
        self._processed_messages = FakeProcessedMessageRepository(
            store, self._staged_processed_messages, clock=clock
        )
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
    def booking_actions(self) -> BookingActionRepositoryPort:
        """Active booking action repository."""
        return self._booking_actions

    @property
    def business_hours(self) -> BusinessHoursRepositoryPort:
        """Active business hours repository."""
        return self._business_hours

    @property
    def conversation_states(self) -> ConversationStateRepositoryPort:
        """Active conversation state repository."""
        return self._conversation_states

    @property
    def customers(self) -> CustomerRepositoryPort:
        """Active customer repository."""
        return self._customers

    @property
    def processed_messages(self) -> ProcessedMessageRepositoryPort:
        """Active processed message repository."""
        return self._processed_messages

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
            self._store.booking_actions = self._staged_booking_actions
            self._store.conversation_states = self._staged_conversation_states
            self._store.customers = self._staged_customers
            self._store.processed_messages = self._staged_processed_messages


class FakeUnitOfWorkFactory:
    """Factory producing FakeUnitOfWork instances sharing a data store."""

    def __init__(
        self,
        store: InMemoryDataStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Initialize with optional existing store or create a new one."""
        self.store = store or InMemoryDataStore()
        self.clock = clock

    def __call__(self) -> UnitOfWork:
        """Create and return a new FakeUnitOfWork."""
        return FakeUnitOfWork(self.store, clock=self.clock)
