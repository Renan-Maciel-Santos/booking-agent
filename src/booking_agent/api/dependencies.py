"""Dependency-injection wiring for FastAPI routes (composition root)."""

# Annotated attaches Depends metadata to a type.
from typing import Annotated

# Depends declares injectable parameters; Request exposes app.state.
from fastapi import Depends, Request

# Engine type stored on app.state during startup.
from sqlalchemy.ext.asyncio import AsyncEngine

# Settings type stored on app.state by the factory.
from booking_agent.config import Settings

# Concrete repository used in production.
from booking_agent.repositories.health_repository import HealthRepository

# Service exposed to the routers.
from booking_agent.services.health_service import HealthService


def get_engine(request: Request) -> AsyncEngine:
    """Return the engine created in the application lifespan.

    Args:
        request: Current HTTP request.

    Returns:
        Shared AsyncEngine.
    """
    # The lifespan stores exactly one engine per application instance.
    engine: AsyncEngine = request.app.state.engine
    # Hand it to downstream dependencies.
    return engine


def get_app_settings(request: Request) -> Settings:
    """Return the settings the application was created with.

    Reading from app.state (not the environment) keeps routes consistent with
    settings injected into create_app, e.g. in tests.

    Args:
        request: Current HTTP request.

    Returns:
        Application settings.
    """
    # The factory stores the settings it was built with.
    settings: Settings = request.app.state.settings
    # Hand them to downstream dependencies.
    return settings


def get_health_service(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HealthService:
    """Build the health service with its production dependencies.

    Args:
        engine: Shared database engine.
        settings: Application settings.

    Returns:
        Ready-to-use HealthService.
    """
    # Constructor injection: the service only knows the repository contract.
    return HealthService(
        database_checker=HealthRepository(engine),
        timeout_seconds=settings.db_health_timeout_seconds,
    )


# Reusable alias so routes declare the dependency in one short line.
HealthServiceDependency = Annotated[HealthService, Depends(get_health_service)]
