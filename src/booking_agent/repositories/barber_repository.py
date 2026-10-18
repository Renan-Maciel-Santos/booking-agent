"""Repository for Barber persistence."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import Barber


class BarberRepository:
    """Repository managing Barber resources."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_id(self, barber_id: uuid.UUID) -> Barber | None:
        """Fetch a barber by primary key."""
        stmt = select(Barber).where(Barber.id == barber_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Barber | None:
        """Fetch a barber by name."""
        stmt = select(Barber).where(Barber.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self) -> Sequence[Barber]:
        """Return all active barbers ordered by name."""
        stmt = select(Barber).where(Barber.active.is_(True)).order_by(Barber.name)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        name: str,
        active: bool = True,
        barber_id: uuid.UUID | None = None,
    ) -> Barber:
        """Create and flush a new barber.

        Args:
            name: Barber name.
            active: Whether barber is active.
            barber_id: Optional UUID. If None, generated automatically.

        Returns:
            The created barber instance.
        """
        barber = Barber(
            id=barber_id or uuid.uuid4(),
            name=name,
            active=active,
        )
        self._session.add(barber)
        await self._session.flush()
        return barber

    async def update(
        self,
        barber_id: uuid.UUID,
        name: str | None = None,
        active: bool | None = None,
    ) -> Barber | None:
        """Update barber attributes and refresh updated_at.

        Args:
            barber_id: ID of the barber to update.
            name: Optional new name.
            active: Optional new active status.

        Returns:
            Updated barber or None if not found.
        """
        values: dict[str, object] = {"updated_at": func.now()}
        if name is not None:
            values["name"] = name
        if active is not None:
            values["active"] = active

        stmt = (
            update(Barber)
            .where(Barber.id == barber_id)
            .values(**values)
            .returning(Barber)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()
