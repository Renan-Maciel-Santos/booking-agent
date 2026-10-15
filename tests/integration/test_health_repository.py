"""Integration tests against a real PostgreSQL instance."""

# Sync client that runs the app lifespan (startup/shutdown).
from fastapi.testclient import TestClient

# Async engine type.
from sqlalchemy.ext.asyncio import AsyncEngine

# Settings for the app factory.
from booking_agent.config import Settings

# Application factory.
from booking_agent.main import create_app

# Repository under test.
from booking_agent.repositories.health_repository import HealthRepository


async def test_ping_succeeds_against_real_postgres(engine: AsyncEngine) -> None:
    # Arrange the production repository with a real engine.
    repository = HealthRepository(engine)

    # Act + Assert: ping must not raise when the database is up.
    await repository.ping()


def test_ready_endpoint_end_to_end(integration_settings: Settings) -> None:
    # Arrange the full app; the context manager runs the real lifespan.
    with TestClient(create_app(integration_settings)) as client:
        # Act: call readiness through every layer down to PostgreSQL.
        response = client.get("/health/ready")

    # Assert the whole stack reports healthy.
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}
