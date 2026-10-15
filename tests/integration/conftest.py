"""Fixtures that start a real PostgreSQL container via testcontainers."""

# Iterator types for generator fixtures.
from collections.abc import AsyncIterator, Iterator

# pytest fixtures and explicit failures.
import pytest

# Raised by the Docker SDK when the daemon is unreachable.
from docker.errors import DockerException

# Async engine type.
from sqlalchemy.ext.asyncio import AsyncEngine

# Disposable PostgreSQL container.
from testcontainers.community.postgres import PostgresContainer

# Settings and engine factory under test.
from booking_agent.config import Settings
from booking_agent.database.engine import build_engine
from tests.conftest import build_test_settings

# Same major version as docker-compose and production.
POSTGRES_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Start one PostgreSQL container for the whole integration session."""
    try:
        # The constructor already connects to the Docker daemon, so it belongs in the try.
        container = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg")
        # Pull/start the container and wait until it accepts connections.
        container.start()
    except DockerException as error:
        # Fail loudly instead of skipping: missing Docker must never look like a green build.
        pytest.fail(f"Docker is required for integration tests but is unavailable: {error}")
    try:
        # Hand the running container to the tests.
        yield container
    finally:
        # Always remove the container, even if tests fail.
        container.stop()


@pytest.fixture(scope="session")
def integration_settings(postgres_container: PostgresContainer) -> Settings:
    """Settings pointing at the running container."""
    # Map the container's dynamic host/port and credentials into Settings.
    return build_test_settings(
        postgres_user=postgres_container.username,
        postgres_password=postgres_container.password,
        postgres_db=postgres_container.dbname,
        postgres_host=postgres_container.get_container_host_ip(),
        postgres_port=int(postgres_container.get_exposed_port(5432)),
    )


@pytest.fixture
async def engine(integration_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Engine bound to the container, disposed after each test."""
    # Build the engine exactly like production does.
    database_engine = build_engine(integration_settings)
    # Provide it to the test.
    yield database_engine
    # Close pooled connections.
    await database_engine.dispose()
