"""Repository for Appointment persistence and slot exclusivity checks."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import Appointment
from booking_agent.domain.errors import SlotUnavailableError

# PostgreSQL error code for exclusion_violation
_EXCLUSION_VIOLATION_SQLSTATE = "23P01"


def _is_exclusion_violation(exc: IntegrityError) -> bool:
    """Check if the IntegrityError represents a PostgreSQL exclusion constraint violation."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code == _EXCLUSION_VIOLATION_SQLSTATE:
        return True
    return type(orig).__name__ == "ExclusionViolationError"


class AppointmentRepository:
    """Repository managing Appointment persistence."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_id(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Fetch an appointment by primary key."""
        stmt = select(Appointment).where(Appointment.id == appointment_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_customer(self, customer_id: uuid.UUID) -> Sequence[Appointment]:
        """Fetch all appointments for a given customer ordered by starts_at."""
        stmt = (
            select(Appointment)
            .where(Appointment.customer_id == customer_id)
            .order_by(Appointment.starts_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_by_barber(self, barber_id: uuid.UUID) -> Sequence[Appointment]:
        """Fetch all appointments for a given barber ordered by starts_at."""
        stmt = (
            select(Appointment)
            .where(Appointment.barber_id == barber_id)
            .order_by(Appointment.starts_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        barber_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        price_cents: int,
        status: str = "confirmed",
        appointment_id: uuid.UUID | None = None,
    ) -> Appointment:
        """Create and flush an appointment.

        Converts PostgreSQL exclusion_violation (SQLSTATE 23P01) into SlotUnavailableError,
        preserving the underlying exception cause.

        Args:
            customer_id: Foreign key to customer.
            service_id: Foreign key to service.
            barber_id: Foreign key to barber.
            starts_at: Appointment start time in UTC.
            ends_at: Appointment end time in UTC.
            price_cents: Price frozen at booking time.
            status: Appointment status ('confirmed', 'cancelled', 'completed').
            appointment_id: Optional explicit UUID.

        Returns:
            The created appointment instance.

        Raises:
            SlotUnavailableError: If the barber already has an overlapping confirmed appointment.
            IntegrityError: If another foreign key or check constraint is violated.
        """
        appointment = Appointment(
            id=appointment_id or uuid.uuid4(),
            customer_id=customer_id,
            service_id=service_id,
            barber_id=barber_id,
            starts_at=starts_at,
            ends_at=ends_at,
            price_cents=price_cents,
            status=status,
        )
        self._session.add(appointment)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if _is_exclusion_violation(exc):
                raise SlotUnavailableError(
                    f"Barber {barber_id} is unavailable between "
                    f"{starts_at.isoformat()} and {ends_at.isoformat()}."
                ) from exc
            raise

        return appointment

    async def cancel(self, appointment_id: uuid.UUID) -> Appointment | None:
        """Mark an appointment as cancelled and refresh updated_at.

        Args:
            appointment_id: ID of the appointment to cancel.

        Returns:
            Updated appointment or None if not found.
        """
        stmt = (
            update(Appointment)
            .where(Appointment.id == appointment_id)
            .values(status="cancelled", updated_at=func.now())
            .returning(Appointment)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()
