"""Repository for BookingAction idempotency and audit tracking."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import BookingAction
from booking_agent.domain.errors import ActionAlreadyExistsError

_UNIQUE_VIOLATION_SQLSTATE = "23505"
_ACTION_KEY_CONSTRAINT = "pk_booking_actions"


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Extract constraint name from underlying driver exception across asyncpg and psycopg."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None

    # Inspect orig and any underlying DBAPI/asyncpg __cause__
    cause = getattr(orig, "__cause__", None)
    for obj in (orig, cause):
        if obj is None:
            continue
        constraint = getattr(obj, "constraint_name", None)
        if constraint:
            return str(constraint)
        diag = getattr(obj, "diag", None)
        if diag is not None:
            diag_constraint = getattr(diag, "constraint_name", None)
            if diag_constraint:
                return str(diag_constraint)

    return None


def _is_action_key_unique_violation(exc: IntegrityError) -> bool:
    """Check if IntegrityError represents a 23505 violation specifically on pk_booking_actions.

    Only violations of 'pk_booking_actions' are converted to ActionAlreadyExistsError.
    Any other unique constraint violation (such as 'uq_customers_phone' when concurrent
    bookings register the same new customer) must propagate intact as IntegrityError.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code != _UNIQUE_VIOLATION_SQLSTATE and type(orig).__name__ != "UniqueViolationError":
        return False

    constraint = _extract_constraint_name(exc)
    return constraint == _ACTION_KEY_CONSTRAINT


class BookingActionRepository:
    """Repository managing BookingAction persistence and action key idempotency.

    Note on Error Translation:
        Only SQLSTATE 23505 specifically targeting the primary key constraint
        ('pk_booking_actions') is translated to ActionAlreadyExistsError.
        All other integrity violations (e.g., FK violations, customer phone unique violations,
        NOT NULL violations, check constraints) are propagated as IntegrityError.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_id(self, action_id: uuid.UUID) -> BookingAction | None:
        """Fetch a booking action record by primary key."""
        stmt = select(BookingAction).where(BookingAction.action_id == action_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def record_action(
        self,
        action_id: uuid.UUID,
        kind: str,
        appointment_id: uuid.UUID,
        result: dict[str, Any],
    ) -> BookingAction:
        """Record an executed booking mutation within the active transaction.

        Args:
            action_id: Unique action identifier UUID.
            kind: Kind of action ('book', 'reschedule', 'cancel').
            appointment_id: Target appointment UUID.
            result: Serialized result payload.

        Returns:
            The created BookingAction instance.

        Raises:
            ActionAlreadyExistsError: If an action with the action_id already exists (23505 on PK).
            IntegrityError: If another constraint is violated.
        """
        action = BookingAction(
            action_id=action_id,
            kind=kind,
            appointment_id=appointment_id,
            result=result,
        )
        self._session.add(action)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if _is_action_key_unique_violation(exc):
                raise ActionAlreadyExistsError(str(action_id)) from exc
            raise

        return action
