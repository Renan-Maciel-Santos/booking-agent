"""BookingService orchestration layer managing scheduling, availability, and transactions."""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from booking_agent.config import Settings
from booking_agent.domain.enums import AppointmentStatus
from booking_agent.domain.errors import (
    AppointmentInThePastError,
    AppointmentNotConfirmedError,
    AppointmentNotFoundError,
    BarberNotFoundError,
    InvalidLocalTimeError,
    InvalidTimeWindowError,
    MinimumNoticeViolationError,
    NotAppointmentOwnerError,
    OutsideBusinessHoursError,
    ServiceNotFoundError,
    SlotInThePastError,
    SlotUnavailableError,
)
from booking_agent.domain.models import (
    AvailabilityResult,
    BookingResult,
    CancelResult,
    RescheduleResult,
)
from booking_agent.domain.ports import Clock, UnitOfWorkFactory

logger = logging.getLogger(__name__)


def localize_datetime(
    target_date: date,
    target_time: time,
    tz: ZoneInfo,
) -> datetime:
    """Convert local calendar date and time into a timezone-aware datetime in the target timezone.

    Detects non-existent wall-clock times during spring-forward daylight saving gaps
    and raises InvalidLocalTimeError. Resolves ambiguous wall-clock times during fall-back
    transitions deterministically using fold=0 (the first/standard occurrence).

    Note:
        Brazil has not observed daylight saving time since 2019. This conversion logic
        and error handling ensure correctness across any configured business timezone.

    Args:
        target_date: Calendar date in the business local calendar.
        target_time: Time of day in the business local clock.
        tz: Target business ZoneInfo.

    Returns:
        Timezone-aware datetime localized to the target timezone.

    Raises:
        InvalidLocalTimeError: If the local time does not exist due to a DST gap.
    """
    dt_local = datetime.combine(target_date, target_time, tzinfo=tz).replace(fold=0)

    # Detect non-existent spring-forward times via round-trip conversion to UTC
    dt_utc = dt_local.astimezone(UTC)
    dt_roundtrip = dt_utc.astimezone(tz)
    if (
        dt_roundtrip.year != target_date.year
        or dt_roundtrip.month != target_date.month
        or dt_roundtrip.day != target_date.day
        or dt_roundtrip.hour != target_time.hour
        or dt_roundtrip.minute != target_time.minute
    ):
        raise InvalidLocalTimeError(
            f"Local time {target_date.isoformat()} {target_time.isoformat()} does not exist "
            f"in timezone {tz.key} due to daylight saving transition."
        )

    return dt_local


class BookingService:
    """Orchestrates appointment availability, booking, rescheduling, and cancellation."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        settings: Settings,
    ) -> None:
        """Store injected collaborators.

        Args:
            uow_factory: Factory producing UnitOfWork context managers.
            clock: Time provider for current datetime.
            settings: Application scheduling configuration.
        """
        self._uow_factory = uow_factory
        self._clock = clock
        self._settings = settings
        self._business_tz = ZoneInfo(settings.business_timezone)

    @property
    def business_timezone(self) -> ZoneInfo:
        """Return the configured business timezone."""
        return self._business_tz

    def localize_datetime(self, target_date: date, target_time: time) -> datetime:
        """Convert local date and time to business timezone datetime."""
        return localize_datetime(target_date, target_time, self._business_tz)

    async def list_availability(
        self,
        service_id: uuid.UUID,
        target_date: date,
        barber_id: uuid.UUID | None = None,
    ) -> AvailabilityResult:
        """Calculate available start times for a service on a given local calendar date.

        Args:
            service_id: ID of the active service being booked.
            target_date: Local calendar date in the salon's timezone.
            barber_id: Optional specific barber ID. If None, unions slots across all active barbers.

        Returns:
            AvailabilityResult containing available local timezone-aware start datetimes.

        Raises:
            ServiceNotFoundError: If the service does not exist or is inactive.
            BarberNotFoundError: If the specific barber does not exist or is inactive.
            InvalidTimeWindowError: If target_date is in the past or exceeds max horizon.
        """
        now_utc = self._clock.now()
        today_local = now_utc.astimezone(self._business_tz).date()

        if target_date < today_local:
            raise InvalidTimeWindowError(f"Target date {target_date.isoformat()} is in the past.")

        max_horizon_days = self._settings.max_booking_horizon_days
        max_date = today_local + timedelta(days=max_horizon_days)
        if target_date > max_date:
            raise InvalidTimeWindowError(
                f"Target date {target_date.isoformat()} exceeds maximum booking horizon "
                f"of {max_horizon_days} days."
            )

        async with self._uow_factory() as uow:
            service = await uow.services.get_by_id(service_id)
            if service is None or not service.active:
                raise ServiceNotFoundError(f"Service {service_id} not found or inactive.")

            if barber_id is not None:
                barber = await uow.barbers.get_by_id(barber_id)
                if barber is None or not barber.active:
                    raise BarberNotFoundError(f"Barber {barber_id} not found or inactive.")
                active_barbers = [barber]
            else:
                active_barbers = list(await uow.barbers.list_active())
                if not active_barbers:
                    return AvailabilityResult(
                        service_id=service_id,
                        target_date=target_date,
                        slots=[],
                        barber_id=barber_id,
                    )

            hours = await uow.business_hours.get_by_day(target_date.weekday())
            if hours is None:
                # Salon is closed on this day
                return AvailabilityResult(
                    service_id=service_id,
                    target_date=target_date,
                    slots=[],
                    barber_id=barber_id,
                )

            opens_at = hours.opens_at
            closes_at = hours.closes_at
            granularity = timedelta(minutes=self._settings.slot_granularity_minutes)
            duration = timedelta(minutes=service.duration_minutes)
            min_advance = timedelta(minutes=self._settings.min_booking_advance_minutes)

            # Generate candidate slots within local business hours
            day_open_dt = datetime.combine(target_date, opens_at, tzinfo=self._business_tz)
            day_close_dt = datetime.combine(target_date, closes_at, tzinfo=self._business_tz)

            candidate_slots: list[datetime] = []
            current_start = day_open_dt
            while current_start + duration <= day_close_dt:
                current_start_utc = current_start.astimezone(UTC)
                if current_start_utc >= now_utc + min_advance:
                    candidate_slots.append(current_start)
                current_start += granularity

            if not candidate_slots:
                return AvailabilityResult(
                    service_id=service_id,
                    target_date=target_date,
                    slots=[],
                    barber_id=barber_id,
                )

            # Window query bounds in UTC covering the business day
            window_start_utc = day_open_dt.astimezone(UTC)
            window_end_utc = day_close_dt.astimezone(UTC)

            # Check slot availability across candidate barbers
            available_slots_set: set[datetime] = set()

            for b in active_barbers:
                existing_appts = await uow.appointments.list_by_barber_and_window(
                    b.id, window_start_utc, window_end_utc
                )
                for slot in candidate_slots:
                    slot_start_utc = slot.astimezone(UTC)
                    slot_end_utc = slot_start_utc + duration
                    # Check if candidate overlaps existing appointment interval
                    has_conflict = any(
                        slot_start_utc < appt.ends_at and slot_end_utc > appt.starts_at
                        for appt in existing_appts
                    )
                    if not has_conflict:
                        available_slots_set.add(slot)

            sorted_slots = sorted(available_slots_set)
            return AvailabilityResult(
                service_id=service_id,
                target_date=target_date,
                slots=sorted_slots,
                barber_id=barber_id,
            )

    async def book(
        self,
        customer_phone: str,
        customer_name: str,
        service_id: uuid.UUID,
        starts_at: datetime,
        barber_id: uuid.UUID | None = None,
    ) -> BookingResult:
        """Book an appointment, resolving barber assignment and handling concurrency conflicts.

        Args:
            customer_phone: E.164 customer phone number.
            customer_name: Full customer name.
            service_id: ID of the service.
            starts_at: Timezone-aware appointment start datetime.
            barber_id: Optional barber ID. If None, deterministically selects an available barber.

        Returns:
            BookingResult with the persisted appointment.

        Raises:
            ValueError: If starts_at is not timezone-aware.
            SlotInThePastError: If starts_at is in the past.
            MinimumNoticeViolationError: If advance notice is insufficient.
            InvalidTimeWindowError: If starts_at exceeds maximum horizon.
            OutsideBusinessHoursError: If slot falls outside business hours.
            ServiceNotFoundError: If service is missing or inactive.
            BarberNotFoundError: If requested barber is missing or inactive.
            SlotUnavailableError: If slot is occupied (carries alternative slot recommendations).
        """
        if starts_at.tzinfo is None or starts_at.tzinfo.utcoffset(starts_at) is None:
            raise ValueError("starts_at must be a timezone-aware datetime.")

        now_utc = self._clock.now()
        starts_at_utc = starts_at.astimezone(UTC)

        if starts_at_utc <= now_utc:
            raise SlotInThePastError(f"Appointment start {starts_at.isoformat()} is in the past.")

        min_advance = timedelta(minutes=self._settings.min_booking_advance_minutes)
        if starts_at_utc < now_utc + min_advance:
            raise MinimumNoticeViolationError(
                f"Booking requires at least {self._settings.min_booking_advance_minutes} "
                f"minutes advance notice."
            )

        starts_at_local = starts_at_utc.astimezone(self._business_tz)
        local_date = starts_at_local.date()
        today_local = now_utc.astimezone(self._business_tz).date()
        max_horizon_days = self._settings.max_booking_horizon_days

        if local_date > today_local + timedelta(days=max_horizon_days):
            raise InvalidTimeWindowError(
                f"Booking date {local_date.isoformat()} exceeds maximum booking horizon "
                f"of {max_horizon_days} days."
            )

        attempted_barber_ids: set[uuid.UUID] = set()
        last_conflict_exc: SlotUnavailableError | None = None

        while True:
            selected_barber_id: uuid.UUID | None = None
            try:
                async with self._uow_factory() as uow:
                    service = await uow.services.get_by_id(service_id)
                    if service is None or not service.active:
                        raise ServiceNotFoundError(f"Service {service_id} not found or inactive.")

                    hours = await uow.business_hours.get_by_day(local_date.weekday())
                    if hours is None:
                        day_name = local_date.strftime("%A")
                        raise OutsideBusinessHoursError(
                            f"Salon is closed on {day_name} ({local_date.isoformat()})."
                        )

                    duration = timedelta(minutes=service.duration_minutes)
                    ends_at_local = starts_at_local + duration
                    if (
                        starts_at_local.time() < hours.opens_at
                        or ends_at_local.time() > hours.closes_at
                        or ends_at_local.date() != local_date
                    ):
                        raise OutsideBusinessHoursError(
                            f"Slot [{starts_at_local.time()}, {ends_at_local.time()}) is outside "
                            f"business hours [{hours.opens_at}, {hours.closes_at})."
                        )

                    ends_at_utc = starts_at_utc + duration

                    # Resolve barber assignment
                    if barber_id is not None:
                        barber = await uow.barbers.get_by_id(barber_id)
                        if barber is None or not barber.active:
                            raise BarberNotFoundError(f"Barber {barber_id} not found or inactive.")
                        selected_barber_id = barber_id
                    else:
                        active_barbers = list(await uow.barbers.list_active())
                        if not active_barbers:
                            raise BarberNotFoundError("No active barbers available.")

                        for b in active_barbers:
                            if b.id in attempted_barber_ids:
                                continue
                            conflicts = await uow.appointments.list_by_barber_and_window(
                                b.id, starts_at_utc, ends_at_utc
                            )
                            if not conflicts:
                                selected_barber_id = b.id
                                break

                        if selected_barber_id is None:
                            last_conflict_exc = SlotUnavailableError(
                                f"No barber is available between {starts_at.isoformat()} "
                                f"and {ends_at_local.isoformat()}."
                            )
                            break

                    # Ensure customer profile exists or create JIT. Preserve existing name
                    # if the profile already exists (e.g. shared family phones).
                    customer = await uow.customers.get_by_phone(customer_phone)
                    if customer is None:
                        customer = await uow.customers.create(
                            name=customer_name, phone=customer_phone
                        )

                    if selected_barber_id is None:
                        raise BarberNotFoundError("No active barbers available.")

                    # Create appointment (exclusion constraint protects against concurrent races)
                    appt = await uow.appointments.create(
                        customer_id=customer.id,
                        service_id=service.id,
                        barber_id=selected_barber_id,
                        starts_at=starts_at_utc,
                        ends_at=ends_at_utc,
                        price_cents=service.price_cents,
                    )
                    return BookingResult(appointment=appt)

            except SlotUnavailableError as exc:
                last_conflict_exc = exc
                if barber_id is not None:
                    break
                if selected_barber_id is not None:
                    attempted_barber_ids.add(selected_barber_id)
                logger.info(
                    "Conflict on barber %s for slot %s; retrying next free barber.",
                    selected_barber_id,
                    starts_at_utc.isoformat(),
                )
                continue

        # UoW context manager has rolled back and closed the aborted transaction.
        # Query alternatives in a new unit of work session:
        logger.info("Conflict on slot %s; querying alternatives.", starts_at_utc.isoformat())
        alt_result = await self.list_availability(
            service_id=service_id,
            target_date=local_date,
            barber_id=barber_id,
        )
        alternatives = list(alt_result.slots[:3])
        if last_conflict_exc is not None:
            raise SlotUnavailableError(
                "Requested appointment slot is unavailable.",
                alternatives=alternatives,
            ) from last_conflict_exc
        raise SlotUnavailableError(
            "Requested appointment slot is unavailable.",
            alternatives=alternatives,
        )

    async def reschedule(
        self,
        appointment_id: uuid.UUID,
        customer_phone: str,
        new_starts_at: datetime,
    ) -> RescheduleResult:
        """Reschedule an existing appointment via in-place UPDATE.

        Args:
            appointment_id: ID of the appointment to reschedule.
            customer_phone: E.164 phone of the customer requesting reschedule.
            new_starts_at: Timezone-aware new start datetime.

        Returns:
            RescheduleResult with the updated appointment.

        Raises:
            ValueError: If new_starts_at is not timezone-aware.
            SlotInThePastError: If new_starts_at is in the past.
            MinimumNoticeViolationError: If advance notice is insufficient.
            InvalidTimeWindowError: If new_starts_at exceeds maximum horizon.
            AppointmentNotFoundError: If appointment is not found.
            NotAppointmentOwnerError: If appointment belongs to another customer.
            AppointmentNotConfirmedError: If appointment is not confirmed.
            AppointmentInThePastError: If existing appointment has already passed.
            OutsideBusinessHoursError: If new slot falls outside business hours.
            SlotUnavailableError: If new slot is occupied by another booking.
        """
        if new_starts_at.tzinfo is None or new_starts_at.tzinfo.utcoffset(new_starts_at) is None:
            raise ValueError("new_starts_at must be a timezone-aware datetime.")

        now_utc = self._clock.now()
        new_starts_at_utc = new_starts_at.astimezone(UTC)

        if new_starts_at_utc <= now_utc:
            raise SlotInThePastError(
                f"New appointment start {new_starts_at.isoformat()} is in the past."
            )

        min_advance = timedelta(minutes=self._settings.min_booking_advance_minutes)
        if new_starts_at_utc < now_utc + min_advance:
            raise MinimumNoticeViolationError(
                f"Rescheduling requires at least {self._settings.min_booking_advance_minutes} "
                f"minutes advance notice."
            )

        new_starts_at_local = new_starts_at_utc.astimezone(self._business_tz)
        local_date = new_starts_at_local.date()
        today_local = now_utc.astimezone(self._business_tz).date()
        max_horizon_days = self._settings.max_booking_horizon_days

        if local_date > today_local + timedelta(days=max_horizon_days):
            raise InvalidTimeWindowError(
                f"Reschedule date {local_date.isoformat()} exceeds maximum booking horizon "
                f"of {max_horizon_days} days."
            )

        appt_service_id: uuid.UUID | None = None
        appt_barber_id: uuid.UUID | None = None

        try:
            async with self._uow_factory() as uow:
                appt = await uow.appointments.get_by_id(appointment_id)
                if appt is None:
                    raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")

                appt_service_id = appt.service_id
                appt_barber_id = appt.barber_id

                customer = await uow.customers.get_by_id(appt.customer_id)
                if customer is None or customer.phone != customer_phone:
                    raise NotAppointmentOwnerError(
                        f"Appointment {appointment_id} does not belong to {customer_phone}."
                    )

                if appt.status != AppointmentStatus.CONFIRMED.value:
                    raise AppointmentNotConfirmedError(
                        f"Cannot reschedule appointment with status '{appt.status}'."
                    )

                if appt.starts_at <= now_utc:
                    raise AppointmentInThePastError(
                        f"Cannot reschedule an appointment that has already passed "
                        f"({appt.starts_at.isoformat()})."
                    )

                service = await uow.services.get_by_id(appt.service_id)
                if service is None:
                    raise ServiceNotFoundError(f"Service {appt.service_id} not found.")

                hours = await uow.business_hours.get_by_day(local_date.weekday())
                if hours is None:
                    day_name = local_date.strftime("%A")
                    raise OutsideBusinessHoursError(
                        f"Salon is closed on {day_name} ({local_date.isoformat()})."
                    )

                duration = timedelta(minutes=service.duration_minutes)
                new_ends_at_local = new_starts_at_local + duration
                if (
                    new_starts_at_local.time() < hours.opens_at
                    or new_ends_at_local.time() > hours.closes_at
                    or new_ends_at_local.date() != local_date
                ):
                    slot_repr = f"[{new_starts_at_local.time()}, {new_ends_at_local.time()})"
                    hours_repr = f"[{hours.opens_at}, {hours.closes_at})"
                    raise OutsideBusinessHoursError(
                        f"Slot {slot_repr} is outside business hours {hours_repr}."
                    )

                new_ends_at_utc = new_starts_at_utc + duration

                updated = await uow.appointments.reschedule(
                    appointment_id=appointment_id,
                    starts_at=new_starts_at_utc,
                    ends_at=new_ends_at_utc,
                )
                if updated is None:
                    raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")

                return RescheduleResult(appointment=updated)

        except SlotUnavailableError as exc:
            # Aborted transaction rolled back by UoW __aexit__; query alternatives in new session
            logger.info(
                "Conflict on reschedule %s; querying alternatives.",
                new_starts_at.isoformat(),
            )
            alternatives: list[datetime] = []
            if appt_service_id is not None:
                alt_result = await self.list_availability(
                    service_id=appt_service_id,
                    target_date=local_date,
                    barber_id=appt_barber_id,
                )
                alternatives = list(alt_result.slots[:3])
            raise SlotUnavailableError(
                "Requested reschedule slot is unavailable.",
                alternatives=alternatives,
            ) from exc

    async def cancel(
        self,
        appointment_id: uuid.UUID,
        customer_phone: str,
    ) -> CancelResult:
        """Cancel an existing appointment. Operation is idempotent for already cancelled bookings.

        Args:
            appointment_id: ID of the appointment to cancel.
            customer_phone: E.164 phone of the customer requesting cancellation.

        Returns:
            CancelResult with the cancelled appointment.

        Raises:
            AppointmentNotFoundError: If appointment is not found.
            NotAppointmentOwnerError: If appointment belongs to another customer.
            AppointmentInThePastError: If appointment start time has already passed.
        """
        now_utc = self._clock.now()

        async with self._uow_factory() as uow:
            appt = await uow.appointments.get_by_id(appointment_id)
            if appt is None:
                raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")

            customer = await uow.customers.get_by_id(appt.customer_id)
            if customer is None or customer.phone != customer_phone:
                raise NotAppointmentOwnerError(
                    f"Appointment {appointment_id} does not belong to customer {customer_phone}."
                )

            # Idempotent cancel: return if already cancelled
            if appt.status == AppointmentStatus.CANCELLED.value:
                return CancelResult(appointment=appt)

            if appt.starts_at <= now_utc:
                raise AppointmentInThePastError(
                    f"Cannot cancel an appointment that has already passed "
                    f"({appt.starts_at.isoformat()})."
                )

            cancelled = await uow.appointments.cancel(appointment_id)
            if cancelled is None:
                raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")

            return CancelResult(appointment=cancelled)
