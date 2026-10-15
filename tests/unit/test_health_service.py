"""Unit tests for HealthService using a fake database checker."""

import pytest
from sqlalchemy.exc import OperationalError

from booking_agent.services.health_service import ComponentStatus, HealthService, HealthStatus
from tests.fakes.fake_health_checker import FakeHealthChecker

# Short timeout keeps the timeout test fast.
FAST_TIMEOUT_SECONDS = 0.05
# Delay safely above the timeout to force it to trigger.
SLOW_DATABASE_DELAY_SECONDS = 0.5


def test_liveness_is_ok_without_touching_the_database() -> None:
    # Arrange a checker that would fail if it were called.
    checker = FakeHealthChecker(error=OSError("must not be called"))
    service = HealthService(checker, timeout_seconds=FAST_TIMEOUT_SECONDS)

    # Act: run the liveness check.
    report = service.check_liveness()

    # Assert the process is reported alive.
    assert report.status is HealthStatus.OK
    # Assert liveness never depends on the database.
    assert checker.ping_calls == 0


async def test_readiness_is_ok_when_database_answers() -> None:
    # Arrange a healthy database.
    service = HealthService(FakeHealthChecker(), timeout_seconds=FAST_TIMEOUT_SECONDS)

    # Act: run the readiness check.
    report = await service.check_readiness()

    # Assert overall and per-component status.
    assert report.status is HealthStatus.OK
    assert report.checks == {"database": ComponentStatus.OK}


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception("connection refused")),
        ConnectionRefusedError("connection refused"),
    ],
    ids=["sqlalchemy-error", "os-error"],
)
async def test_readiness_is_degraded_when_database_fails(error: BaseException) -> None:
    # Arrange a database raising the parametrized error.
    service = HealthService(FakeHealthChecker(error=error), timeout_seconds=FAST_TIMEOUT_SECONDS)

    # Act: run the readiness check.
    report = await service.check_readiness()

    # Assert the failure is reported, not raised.
    assert report.status is HealthStatus.DEGRADED
    assert report.checks == {"database": ComponentStatus.UNAVAILABLE}


async def test_readiness_is_degraded_when_database_times_out() -> None:
    # Arrange a database slower than the allowed timeout.
    checker = FakeHealthChecker(delay_seconds=SLOW_DATABASE_DELAY_SECONDS)
    service = HealthService(checker, timeout_seconds=FAST_TIMEOUT_SECONDS)

    # Act: run the readiness check.
    report = await service.check_readiness()

    # Assert the timeout is treated as unavailability.
    assert report.checks == {"database": ComponentStatus.UNAVAILABLE}


async def test_unexpected_errors_are_not_swallowed() -> None:
    # Arrange a programming error that is not a database outage.
    service = HealthService(
        FakeHealthChecker(error=ValueError("bug")), timeout_seconds=FAST_TIMEOUT_SECONDS
    )

    # Assert bugs propagate instead of being disguised as "database unavailable".
    with pytest.raises(ValueError, match="bug"):
        await service.check_readiness()
