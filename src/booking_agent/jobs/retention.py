"""Entrypoint for the standalone personal data retention purge job.

Triggered via CLI:
    python -m booking_agent.jobs.retention
"""

import asyncio
import logging
import sys

from booking_agent.config import Settings, get_settings
from booking_agent.database.engine import build_engine, build_session_factory
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.ports import Clock
from booking_agent.integrations.system_clock import SystemClock
from booking_agent.logging_config import configure_logging
from booking_agent.services.retention_service import RetentionReport, RetentionService

logger = logging.getLogger("booking_agent.jobs.retention")


async def run_retention_job(
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> RetentionReport:
    """Execute the data retention service with production dependencies.

    Args:
        settings: Optional settings override; defaults to get_settings().
        clock: Optional clock override; defaults to SystemClock().

    Returns:
        RetentionReport detailing affected records.
    """
    app_settings = settings or get_settings()
    engine = build_engine(app_settings)
    try:
        session_factory = build_session_factory(engine)
        uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
        active_clock = clock or SystemClock()
        service = RetentionService(
            uow_factory=uow_factory,
            clock=active_clock,
            settings=app_settings,
        )
        return await service.run()
    finally:
        await engine.dispose()


def main() -> None:
    """CLI entry point for running data retention."""
    settings = get_settings()
    configure_logging(settings.log_level)

    try:
        report = asyncio.run(run_retention_job(settings=settings))
        logger.info(
            "data_retention_completed",
            extra={
                "event": "data_retention_completed",
                "redacted_responses": report.redacted_responses,
                "deleted_messages": report.deleted_messages,
                "deleted_conversation_states": report.deleted_conversation_states,
                "redact_cutoff": (
                    report.redact_cutoff.isoformat() if report.redact_cutoff else None
                ),
                "delete_messages_cutoff": (
                    report.delete_messages_cutoff.isoformat()
                    if report.delete_messages_cutoff
                    else None
                ),
                "delete_conversations_cutoff": (
                    report.delete_conversations_cutoff.isoformat()
                    if report.delete_conversations_cutoff
                    else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "data_retention_failed",
            extra={
                "event": "data_retention_failed",
                "error_type": type(exc).__name__,
            },
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
