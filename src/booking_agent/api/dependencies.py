"""Dependency-injection wiring for FastAPI routes (composition root)."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from booking_agent.config import Settings
from booking_agent.database.engine import build_session_factory
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.integrations.system_clock import SystemClock
from booking_agent.repositories.health_repository import HealthRepository
from booking_agent.services.booking_service import BookingService
from booking_agent.services.health_service import HealthService


def get_engine(request: Request) -> AsyncEngine:
    """Return the engine created in the application lifespan.

    Args:
        request: Current HTTP request.

    Returns:
        Shared AsyncEngine.
    """
    engine: AsyncEngine = request.app.state.engine
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
    settings: Settings = request.app.state.settings
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
    return HealthService(
        database_checker=HealthRepository(engine),
        timeout_seconds=settings.db_health_timeout_seconds,
    )


HealthServiceDependency = Annotated[HealthService, Depends(get_health_service)]


def get_booking_service(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> BookingService:
    """Build the booking service with its production dependencies.

    Args:
        engine: Shared database engine.
        settings: Application settings.

    Returns:
        Ready-to-use BookingService.
    """
    session_factory = build_session_factory(engine)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    return BookingService(
        uow_factory=uow_factory,
        clock=SystemClock(),
        settings=settings,
    )


BookingServiceDependency = Annotated[BookingService, Depends(get_booking_service)]
