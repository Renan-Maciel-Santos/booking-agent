"""Repository for ConversationState persistence and locking."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ConversationState


class ConversationStateRepository:
    """Repository managing dialogue state per phone number."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get(self, phone: str, for_update: bool = False) -> ConversationState | None:
        """Fetch dialogue state for a phone number.

        Args:
            phone: E.164 phone number.
            for_update: If True, locks the row with SELECT ... FOR UPDATE.

        Returns:
            The conversation state or None if not found.
        """
        stmt = select(ConversationState).where(ConversationState.phone == phone)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        phone: str,
        stage: str,
        slots: dict[str, Any],
    ) -> ConversationState:
        """Insert or update conversation state, refreshing updated_at on conflict.

        Args:
            phone: E.164 phone number.
            stage: Dialogue stage (e.g. 'idle', 'collecting_info').
            slots: Extracted entity slots dictionary.

        Returns:
            The upserted conversation state instance.
        """
        stmt = (
            pg_insert(ConversationState)
            .values(phone=phone, stage=stage, slots=slots)
            .on_conflict_do_update(
                index_elements=[ConversationState.phone],
                set_={
                    "stage": stage,
                    "slots": slots,
                    "updated_at": func.now(),
                },
            )
            .returning(ConversationState)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()
