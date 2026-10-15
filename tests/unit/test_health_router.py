"""Unit tests for the health routes with the service replaced by a fake."""

from httpx import ASGITransport, AsyncClient

from booking_agent.api.dependencies import get_health_service
from booking_agent.config import Settings
from booking_agent.main import create_app
from booking_agent.services.health_service import HealthService
from tests.fakes.fake_health_checker import FakeHealthChecker

# Timeout value used by the services built in this module.
TEST_TIMEOUT_SECONDS = 0.05
# Base URL required by httpx; never resolved over the network.
BASE_URL = "http://testserver"


def _client_with(service: HealthService, settings: Settings) -> AsyncClient:
    """Create a client whose app uses the given service.

    Args:
        service: Service to inject instead of the production one.
        settings: Settings for the app factory.

    Returns:
        AsyncClient bound to the in-process app.
    """
    # Build the app without running the lifespan (no engine is created).
    app = create_app(settings)
    # Swap the production service for the test one.
    app.dependency_overrides[get_health_service] = lambda: service
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL)


async def test_live_returns_200(test_settings: Settings) -> None:
    # Arrange a service whose database is down; liveness must not care.
    service = HealthService(
        FakeHealthChecker(error=OSError("down")), timeout_seconds=TEST_TIMEOUT_SECONDS
    )

    # Act: call the liveness endpoint.
    async with _client_with(service, test_settings) as client:
        response = await client.get("/health/live")

    # Assert success with an empty checks map.
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {}}


async def test_ready_returns_200_when_database_is_up(test_settings: Settings) -> None:
    # Arrange a healthy database.
    service = HealthService(FakeHealthChecker(), timeout_seconds=TEST_TIMEOUT_SECONDS)

    # Act: call both the readiness endpoint and its /health alias.
    async with _client_with(service, test_settings) as client:
        ready_response = await client.get("/health/ready")
        alias_response = await client.get("/health")

    # Assert both answer identically with HTTP 200.
    expected_body = {"status": "ok", "checks": {"database": "ok"}}
    assert ready_response.status_code == 200
    assert ready_response.json() == expected_body
    assert alias_response.json() == expected_body


async def test_ready_returns_503_when_database_is_down(test_settings: Settings) -> None:
    # Arrange an unreachable database.
    service = HealthService(
        FakeHealthChecker(error=OSError("down")), timeout_seconds=TEST_TIMEOUT_SECONDS
    )

    # Act: call the readiness endpoint.
    async with _client_with(service, test_settings) as client:
        response = await client.get("/health/ready")

    # Assert orchestrators are told to stop routing traffic.
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "checks": {"database": "unavailable"}}
