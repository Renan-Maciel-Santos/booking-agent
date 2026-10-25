"""Repository for ConversationState persistence and locking."""

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ConversationState
from booking_agent.domain.conversation_state import (
    ConversationSlots,
    ConversationSnapshot,
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import ConversationStage
from booking_agent.domain.errors import InvalidConversationStateError


class ConversationStateRepository:
    """Repository managing dialogue state per phone number."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get(self, phone: str, for_update: bool = False) -> ConversationSnapshot | None:
        """Fetch dialogue state for a phone number.

        Args:
            phone: E.164 phone number.
            for_update: If True, locks the row with SELECT ... FOR UPDATE.

        Returns:
            The validated conversation snapshot or None if not found.

        Raises:
            InvalidConversationStateError: If the persisted JSON fails schema validation.
        """
        stmt = select(ConversationState).where(ConversationState.phone == phone)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None

        try:
            slots = slots_from_json(row.slots)
        except ValidationError as exc:
            raise InvalidConversationStateError.from_validation_error(exc) from exc

        return ConversationSnapshot(
            stage=ConversationStage(row.stage),
            slots=slots,
            updated_at=row.updated_at,
        )

    async def upsert(
        self,
        phone: str,
        stage: ConversationStage,
        slots: ConversationSlots,
    ) -> ConversationSnapshot:
        """Insert or update conversation state, refreshing updated_at on conflict.

        Args:
            phone: E.164 phone number.
            stage: Typed conversation stage.
            slots: Typed conversation slots domain model.

        Returns:
            The upserted conversation snapshot instance.
        """
        raw_slots = slots_to_json(slots)
        stage_val = stage.value

        stmt = (
            pg_insert(ConversationState)
            .values(phone=phone, stage=stage_val, slots=raw_slots)
            .on_conflict_do_update(
                index_elements=[ConversationState.phone],
                set_={
                    "stage": stage_val,
                    "slots": raw_slots,
                    "updated_at": func.now(),
                },
            )
            .returning(ConversationState)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        row = result.scalar_one()

        return ConversationSnapshot(
            stage=stage,
            slots=slots,
            updated_at=row.updated_at,
        )
