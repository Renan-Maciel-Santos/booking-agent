"""Fixtures that start a real PostgreSQL container via testcontainers."""

from collections.abc import AsyncIterator, Iterator

import pytest
from docker.errors import DockerException
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

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
        container.start()
    except DockerException as error:
        # Fail loudly instead of skipping: missing Docker must never look like a green build.
        pytest.fail(f"Docker is required for integration tests but is unavailable: {error}")
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def integration_settings(postgres_container: PostgresContainer) -> Settings:
    """Settings pointing at the running container."""
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
    database_engine = build_engine(integration_settings)
    yield database_engine
    await database_engine.dispose()
