"""Repository for ProcessedMessage idempotency and atomic acquisition."""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ProcessedMessage
from booking_agent.domain.enums import MessageStatus
from booking_agent.domain.models import MessageAcquisition

_ACQUIRE_STATEMENT = text(
    """
    INSERT INTO processed_messages (message_id, status, created_at, updated_at)
    VALUES (:message_id, :status_processing, now(), now())
    ON CONFLICT (message_id) DO UPDATE
    SET status = :status_processing,
        updated_at = now()
    WHERE processed_messages.status = :status_failed
       OR (
           processed_messages.status = :status_processing
           AND processed_messages.updated_at < now() - make_interval(secs => :timeout_seconds)
       )
    RETURNING message_id
    """
)

_MARK_DONE_STATEMENT = text(
    """
    UPDATE processed_messages
    SET status = :status_done,
        response_text = :response_text,
        updated_at = now()
    WHERE message_id = :message_id
    RETURNING message_id
    """
)

_MARK_FAILED_STATEMENT = text(
    """
    UPDATE processed_messages
    SET status = :status_failed, updated_at = now()
    WHERE message_id = :message_id
    RETURNING message_id
    """
)


class ProcessedMessageRepository:
    """Repository managing webhook message idempotency and distributed lease acquisition."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the injected session.

        Args:
            session: Active SQLAlchemy async session (unit of work).
        """
        self._session = session

    async def get(self, message_id: str) -> ProcessedMessage | None:
        """Fetch a processed message record by primary key."""
        stmt = select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def acquire(self, message_id: str, timeout_seconds: int = 300) -> MessageAcquisition:
        """Atomically acquire processing rights for a message ID.

        Succeeds (acquired=True) if the message was never seen, is in 'failed' status,
        or has been stuck in 'processing' past the timeout interval.
        Returns acquired=False if the message is already 'done' (including its saved response_text)
        or currently 'processing'.

        Args:
            message_id: Webhook message identifier.
            timeout_seconds: Timeout in seconds to consider 'processing' stale.

        Returns:
            MessageAcquisition with acquired flag, status, and optional response_text.
        """
        result = await self._session.execute(
            _ACQUIRE_STATEMENT,
            {
                "message_id": message_id,
                "timeout_seconds": timeout_seconds,
                "status_processing": MessageStatus.PROCESSING.value,
                "status_failed": MessageStatus.FAILED.value,
            },
        )
        await self._session.flush()
        acquired_row = result.scalar_one_or_none()
        if acquired_row is not None:
            return MessageAcquisition(
                acquired=True,
                status=MessageStatus.PROCESSING,
                response_text=None,
            )

        existing = await self.get(message_id)
        if existing is None:
            return MessageAcquisition(
                acquired=False,
                status=MessageStatus.PROCESSING,
                response_text=None,
            )

        return MessageAcquisition(
            acquired=False,
            status=MessageStatus(existing.status),
            response_text=existing.response_text,
        )

    async def mark_done(self, message_id: str, response_text: str | None = None) -> bool:
        """Mark a processed message as done, updating updated_at and saving response_text.

        Args:
            message_id: Webhook message identifier.
            response_text: Final rendered response sent to user.

        Returns:
            True if row was updated, False if message_id not found.
        """
        result = await self._session.execute(
            _MARK_DONE_STATEMENT,
            {
                "message_id": message_id,
                "status_done": MessageStatus.DONE.value,
                "response_text": response_text,
            },
        )
        await self._session.flush()
        return result.scalar_one_or_none() is not None

    async def mark_failed(self, message_id: str) -> bool:
        """Mark a processed message as failed, updating updated_at.

        Args:
            message_id: Webhook message identifier.

        Returns:
            True if row was updated, False if message_id not found.
        """
        result = await self._session.execute(
            _MARK_FAILED_STATEMENT,
            {"message_id": message_id, "status_failed": MessageStatus.FAILED.value},
        )
        await self._session.flush()
        return result.scalar_one_or_none() is not None
