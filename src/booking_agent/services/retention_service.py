"""Service executing the personal data retention policy in batches."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from booking_agent.config import Settings
from booking_agent.domain.ports import Clock, UnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionReport:
    """Summary of affected records per retention policy rule."""

    redacted_responses: int
    deleted_messages: int
    deleted_conversation_states: int
    redact_cutoff: datetime | None = None
    delete_messages_cutoff: datetime | None = None
    delete_conversations_cutoff: datetime | None = None


class RetentionService:
    """Orchestrates batched personal data retention and cleanup."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        settings: Settings,
    ) -> None:
        """Store injected dependencies.

        Args:
            uow_factory: Factory producing UnitOfWork instances.
            clock: Time source for computing policy cutoffs.
            settings: Runtime settings containing retention windows and batch size.
        """
        self._uow_factory = uow_factory
        self._clock = clock
        self._settings = settings

    async def run(self) -> RetentionReport:
        """Execute all retention rules and return an aggregated report.

        Calculates policy cutoffs from clock.now() and executes batched deletions
        and redactions across processed_messages and conversation_states.

        Returns:
            RetentionReport detailing affected records.
        """
        now = self._clock.now()
        redact_cutoff = now - timedelta(days=self._settings.processed_message_redact_after_days)
        delete_messages_cutoff = now - timedelta(
            days=self._settings.processed_message_delete_after_days
        )
        delete_conversations_cutoff = now - timedelta(
            days=self._settings.conversation_state_delete_after_days
        )
        batch_size = self._settings.retention_batch_size

        async with self._uow_factory() as uow:
            redacted_responses = await uow.processed_messages.redact_responses_created_before(
                cutoff=redact_cutoff,
                batch_size=batch_size,
            )
            deleted_messages = await uow.processed_messages.delete_created_before(
                cutoff=delete_messages_cutoff,
                batch_size=batch_size,
            )
            deleted_conversation_states = await uow.conversation_states.delete_inactive_since(
                cutoff=delete_conversations_cutoff,
                batch_size=batch_size,
            )

        return RetentionReport(
            redacted_responses=redacted_responses,
            deleted_messages=deleted_messages,
            deleted_conversation_states=deleted_conversation_states,
            redact_cutoff=redact_cutoff,
            delete_messages_cutoff=delete_messages_cutoff,
            delete_conversations_cutoff=delete_conversations_cutoff,
        )
