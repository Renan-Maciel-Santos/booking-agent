"""Repository for Customer persistence."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import Customer


class CustomerRepository:
    """Repository managing Customer database operations."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get_by_id(self, customer_id: uuid.UUID) -> Customer | None:
        """Fetch a customer by primary key."""
        stmt = select(Customer).where(Customer.id == customer_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Customer | None:
        """Fetch a customer by unique phone number."""
        stmt = select(Customer).where(Customer.phone == phone)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[Customer]:
        """Return all customers ordered by creation time."""
        stmt = select(Customer).order_by(Customer.created_at)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        name: str,
        phone: str,
        customer_id: uuid.UUID | None = None,
    ) -> Customer:
        """Create and flush a new customer record.

        Args:
            name: Customer name.
            phone: E.164 formatted phone number.
            customer_id: Optional UUID. If None, generated automatically.

        Returns:
            The created customer instance.
        """
        customer = Customer(
            id=customer_id or uuid.uuid4(),
            name=name,
            phone=phone,
        )
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def update_name(self, customer_id: uuid.UUID, name: str) -> Customer | None:
        """Update customer name and refresh updated_at.

        Args:
            customer_id: ID of the customer to update.
            name: New customer name.

        Returns:
            Updated customer or None if not found.
        """
        stmt = (
            update(Customer)
            .where(Customer.id == customer_id)
            .values(name=name, updated_at=func.now())
            .returning(Customer)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()
