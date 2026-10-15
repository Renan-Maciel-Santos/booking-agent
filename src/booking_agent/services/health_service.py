"""Application health checks (liveness and readiness)."""

# asyncio.timeout bounds how long the readiness probe may block.
import asyncio

# Module logger; configuration lives in booking_agent.logging_config.
import logging

# Immutable value object returned to the API layer.
from dataclasses import dataclass, field

# StrEnum values serialize naturally to JSON strings.
from enum import StrEnum

# Base class of every SQLAlchemy error (wraps asyncpg errors too).
from sqlalchemy.exc import SQLAlchemyError

# Contract implemented by the health repository.
from booking_agent.repositories.health_repository import DatabaseHealthChecker

# Module-scoped logger named after the module path.
logger = logging.getLogger(__name__)

# Errors that mean "the database is unavailable" rather than "the code is broken".
_DATABASE_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    SQLAlchemyError,
    OSError,
    TimeoutError,
)


class HealthStatus(StrEnum):
    """Overall status of the application."""

    # Every check passed.
    OK = "ok"
    # At least one dependency failed; the process itself is alive.
    DEGRADED = "degraded"


class ComponentStatus(StrEnum):
    """Status of a single dependency."""

    # Dependency answered within the timeout.
    OK = "ok"
    # Dependency failed or timed out.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Result of a health check."""

    # Aggregated status derived from the individual checks.
    status: HealthStatus
    # Per-dependency results keyed by dependency name.
    checks: dict[str, ComponentStatus] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """Whether every check passed."""
        # Healthy means the aggregated status is OK.
        return self.status is HealthStatus.OK


class HealthService:
    """Coordinates health checks for liveness and readiness probes."""

    def __init__(self, database_checker: DatabaseHealthChecker, timeout_seconds: float) -> None:
        """Wire the service with its dependencies.

        Args:
            database_checker: Object able to ping the database.
            timeout_seconds: Maximum time allowed for the database probe.
        """
        # Injected contract, so tests can pass a fake without a database.
        self._database_checker = database_checker
        # Timeout comes from settings; no magic number lives here.
        self._timeout_seconds = timeout_seconds

    def check_liveness(self) -> HealthReport:
        """Report that the process is running.

        Liveness deliberately ignores external dependencies: restarting the app
        would not fix a database outage and could cause a restart loop.

        Returns:
            Always an OK report while the process can answer.
        """
        # Answering at all is the liveness signal.
        return HealthReport(status=HealthStatus.OK)

    async def check_readiness(self) -> HealthReport:
        """Report whether the app can serve traffic (database reachable).

        Returns:
            OK when the database answers in time, DEGRADED otherwise.
        """
        # Run the probe and translate the outcome into a component status.
        database_status = await self._check_database()
        # The app is ready only if the database check passed.
        overall_status = (
            HealthStatus.OK if database_status is ComponentStatus.OK else HealthStatus.DEGRADED
        )
        # Return an immutable report for the API layer to render.
        return HealthReport(status=overall_status, checks={"database": database_status})

    async def _check_database(self) -> ComponentStatus:
        """Ping the database within the configured timeout.

        Returns:
            Component status for the database.
        """
        try:
            # Abort the ping if it exceeds the configured timeout.
            async with asyncio.timeout(self._timeout_seconds):
                # Delegate the actual query to the repository.
                await self._database_checker.ping()
        except _DATABASE_UNAVAILABLE_ERRORS:
            # Log with traceback for diagnosis; the probe response stays generic.
            logger.warning("database health check failed", exc_info=True)
            # Report the dependency as unavailable instead of crashing the endpoint.
            return ComponentStatus.UNAVAILABLE
        # No exception means the database answered in time.
        return ComponentStatus.OK
