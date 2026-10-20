"""Integration tests for ProcessedMessageRepository idempotency and lease acquisition."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.database.models import ProcessedMessage
from booking_agent.domain.enums import MessageStatus
from booking_agent.repositories.processed_message_repository import ProcessedMessageRepository


async def test_acquire_twice_only_first_wins(session: AsyncSession) -> None:
    """Verify that acquiring the same message_id twice only succeeds on the first attempt."""
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"

    # First acquisition succeeds
    first = await repo.acquire(msg_id, timeout_seconds=300)
    assert first.acquired is True
    assert first.status == MessageStatus.PROCESSING
    assert bool(first) is True

    # Immediate second acquisition fails
    second = await repo.acquire(msg_id, timeout_seconds=300)
    assert second.acquired is False
    assert second.status == MessageStatus.PROCESSING
    assert bool(second) is False


async def test_failed_message_can_be_reacquired(session: AsyncSession) -> None:
    """Verify that a message in 'failed' status can be reacquired immediately."""
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"

    # Initial acquire and mark failed
    await repo.acquire(msg_id, timeout_seconds=300)
    await repo.mark_failed(msg_id)
    await session.commit()

    # Re-acquire must succeed
    reacquired = await repo.acquire(msg_id, timeout_seconds=300)
    assert reacquired.acquired is True
    assert reacquired.status == MessageStatus.PROCESSING

    record = await repo.get(msg_id)
    assert record is not None
    assert record.status == "processing"


async def test_processing_within_timeout_cannot_be_reacquired(session: AsyncSession) -> None:
    """Verify that a message currently 'processing' within the timeout cannot be acquired."""
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"

    await repo.acquire(msg_id, timeout_seconds=300)
    await session.commit()

    # Attempt re-acquisition while active
    acquisition = await repo.acquire(msg_id, timeout_seconds=300)
    assert acquisition.acquired is False
    assert acquisition.status == MessageStatus.PROCESSING


async def test_stale_processing_message_can_be_reacquired(session: AsyncSession) -> None:
    """Verify that a message stuck in 'processing' past its timeout can be reacquired."""
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"

    await repo.acquire(msg_id, timeout_seconds=60)
    await session.commit()

    # Manually backdate updated_at beyond the 60-second timeout
    stale_time = datetime.now(UTC) - timedelta(seconds=120)
    await session.execute(
        update(ProcessedMessage)
        .where(ProcessedMessage.message_id == msg_id)
        .values(updated_at=stale_time)
    )
    await session.commit()

    # Re-acquisition must succeed because lease expired
    reacquired = await repo.acquire(msg_id, timeout_seconds=60)
    assert reacquired.acquired is True
    assert reacquired.status == MessageStatus.PROCESSING


async def test_done_message_cannot_be_reacquired_and_replays_response_text(
    session: AsyncSession,
) -> None:
    """Verify that a message marked 'done' cannot be reacquired and returns saved response_text."""
    repo = ProcessedMessageRepository(session)
    msg_id = f"msg_{uuid.uuid4().hex}"
    expected_response = "Agendamento confirmado para terça-feira às 14h!"

    first = await repo.acquire(msg_id, timeout_seconds=60)
    assert first.acquired is True

    await repo.mark_done(msg_id, response_text=expected_response)
    await session.commit()

    reacquisition = await repo.acquire(msg_id, timeout_seconds=60)
    assert reacquisition.acquired is False
    assert reacquisition.status == MessageStatus.DONE
    assert reacquisition.response_text == expected_response
