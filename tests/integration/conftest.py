"""Fixtures that start a real PostgreSQL container via testcontainers."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from booking_agent.config import Settings, get_settings
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

    # Set process environment variables so alembic and get_settings() point to the container.
    os.environ["POSTGRES_USER"] = container.username
    os.environ["POSTGRES_PASSWORD"] = container.password
    os.environ["POSTGRES_DB"] = container.dbname
    os.environ["POSTGRES_HOST"] = container.get_container_host_ip()
    os.environ["POSTGRES_PORT"] = str(container.get_exposed_port(5432))
    os.environ["LLM_PROVIDER"] = "fake"
    get_settings.cache_clear()

    try:
        yield container
    finally:
        container.stop()
        os.environ.pop("LLM_PROVIDER", None)
        get_settings.cache_clear()


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


@pytest.fixture(scope="session")
def alembic_config(integration_settings: Settings) -> Config:
    """Alembic configuration pointing at the project root."""
    ini_path = Path(__file__).parents[2] / "alembic.ini"
    cfg = Config(str(ini_path))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(postgres_container: PostgresContainer, alembic_config: Config) -> None:
    """Ensure database schema is upgraded to head before running integration tests."""
    command.upgrade(alembic_config, "head")


@pytest.fixture
async def engine(integration_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Engine bound to the container, disposed after each test."""
    database_engine = build_engine(integration_settings)
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Factory creating async sessions for tests."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Yield an active async session for a single test."""
    async with session_factory() as test_session:
        yield test_session


@pytest.fixture(autouse=True)
async def isolate_database(engine: AsyncEngine) -> None:
    """Isolate integration tests by truncating domain tables before each test runs.

    Using TRUNCATE TABLE ... RESTART IDENTITY CASCADE provides complete per-test data
    isolation in milliseconds without having to destroy/recreate database containers or
    re-run Alembic migrations.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE "
                "appointments, customers, services, barbers, "
                "booking_actions, conversation_states, processed_messages, business_hours "
                "RESTART IDENTITY CASCADE;"
            )
        )
