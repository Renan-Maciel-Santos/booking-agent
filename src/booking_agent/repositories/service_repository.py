"""Repository for Service catalog persistence."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import Service


class ServiceRepository:
    """Repository managing barbershop Service catalog operations."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        """Fetch a service by primary key."""
        stmt = select(Service).where(Service.id == service_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Service | None:
        """Fetch a service by unique name."""
        stmt = select(Service).where(Service.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self) -> Sequence[Service]:
        """Return all active services ordered by name."""
        stmt = select(Service).where(Service.active.is_(True)).order_by(Service.name)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        name: str,
        duration_minutes: int,
        price_cents: int,
        active: bool = True,
        service_id: uuid.UUID | None = None,
    ) -> Service:
        """Create and flush a new service.

        Args:
            name: Service name.
            duration_minutes: Service duration (> 0).
            price_cents: Price in cents (>= 0).
            active: Whether service is actively bookable.
            service_id: Optional UUID. If None, generated automatically.

        Returns:
            The created service instance.
        """
        service = Service(
            id=service_id or uuid.uuid4(),
            name=name,
            duration_minutes=duration_minutes,
            price_cents=price_cents,
            active=active,
        )
        self._session.add(service)
        await self._session.flush()
        return service

    async def update(
        self,
        service_id: uuid.UUID,
        name: str | None = None,
        duration_minutes: int | None = None,
        price_cents: int | None = None,
        active: bool | None = None,
    ) -> Service | None:
        """Update service details and refresh updated_at.

        Args:
            service_id: ID of the service to update.
            name: Optional new name.
            duration_minutes: Optional new duration.
            price_cents: Optional new price in cents.
            active: Optional new active flag.

        Returns:
            Updated service or None if not found.
        """
        values: dict[str, object] = {"updated_at": func.now()}
        if name is not None:
            values["name"] = name
        if duration_minutes is not None:
            values["duration_minutes"] = duration_minutes
        if price_cents is not None:
            values["price_cents"] = price_cents
        if active is not None:
            values["active"] = active

        stmt = (
            update(Service)
            .where(Service.id == service_id)
            .values(**values)
            .returning(Service)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()
