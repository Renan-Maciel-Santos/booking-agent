"""Application health checks (liveness and readiness)."""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError

from booking_agent.domain.ports import DatabaseHealthChecker

logger = logging.getLogger(__name__)

# Errors that mean "the database is unavailable" rather than "the code is broken".
# Anything else (e.g. ValueError) is a bug and must propagate.
_DATABASE_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    SQLAlchemyError,
    OSError,
    TimeoutError,
)


class HealthStatus(StrEnum):
    """Overall status of the application."""

    OK = "ok"
    DEGRADED = "degraded"


class ComponentStatus(StrEnum):
    """Status of a single dependency."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Result of a health check."""

    status: HealthStatus
    checks: dict[str, ComponentStatus] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """Whether every check passed."""
        return self.status is HealthStatus.OK


class HealthService:
    """Coordinates health checks for liveness and readiness probes."""

    def __init__(self, database_checker: DatabaseHealthChecker, timeout_seconds: float) -> None:
        """Wire the service with its dependencies.

        Args:
            database_checker: Object able to ping the database.
            timeout_seconds: Maximum time allowed for the database probe.
        """
        self._database_checker = database_checker
        self._timeout_seconds = timeout_seconds

    def check_liveness(self) -> HealthReport:
        """Report that the process is running.

        Liveness deliberately ignores external dependencies: restarting the app
        would not fix a database outage and could cause a restart loop.

        Returns:
            Always an OK report while the process can answer.
        """
        return HealthReport(status=HealthStatus.OK)

    async def check_readiness(self) -> HealthReport:
        """Report whether the app can serve traffic (database reachable).

        Returns:
            OK when the database answers in time, DEGRADED otherwise.
        """
        database_status = await self._check_database()
        overall_status = (
            HealthStatus.OK if database_status is ComponentStatus.OK else HealthStatus.DEGRADED
        )
        return HealthReport(status=overall_status, checks={"database": database_status})

    async def _check_database(self) -> ComponentStatus:
        """Ping the database within the configured timeout.

        Returns:
            Component status for the database.
        """
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._database_checker.ping()
        except _DATABASE_UNAVAILABLE_ERRORS:
            # Full traceback goes to the logs; the probe response stays generic
            # so internal details are never exposed over HTTP.
            logger.warning("database health check failed", exc_info=True)
            return ComponentStatus.UNAVAILABLE
        return ComponentStatus.OK
