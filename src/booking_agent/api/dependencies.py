"""Dependency-injection wiring for FastAPI routes (composition root)."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from booking_agent.config import Settings
from booking_agent.database.engine import build_session_factory
from booking_agent.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from booking_agent.domain.ports import Clock, IntentParser, UnitOfWorkFactory
from booking_agent.integrations.system_clock import SystemClock
from booking_agent.repositories.health_repository import HealthRepository
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import ConversationService
from booking_agent.services.entity_resolver import EntityResolver
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


def get_uow_factory(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
) -> UnitOfWorkFactory:
    """Build the UnitOfWorkFactory bound to the shared database engine.

    Args:
        engine: Shared database engine.

    Returns:
        UnitOfWorkFactory implementation.
    """
    session_factory = build_session_factory(engine)
    return SqlAlchemyUnitOfWorkFactory(session_factory)


UowFactoryDependency = Annotated[UnitOfWorkFactory, Depends(get_uow_factory)]


def get_clock(request: Request) -> Clock:
    """Return the Clock port, allowing application state overrides in tests.

    Args:
        request: Current HTTP request.

    Returns:
        Clock implementation (defaults to SystemClock).
    """
    clock: Clock = getattr(request.app.state, "clock", None) or SystemClock()
    return clock


ClockDependency = Annotated[Clock, Depends(get_clock)]


def get_intent_parser(request: Request) -> IntentParser:
    """Return the IntentParser created in the application lifespan.

    Args:
        request: Current HTTP request.

    Returns:
        Shared IntentParser implementation.
    """
    parser: IntentParser = request.app.state.intent_parser
    return parser


IntentParserDependency = Annotated[IntentParser, Depends(get_intent_parser)]


def get_booking_service(
    uow_factory: Annotated[UnitOfWorkFactory, Depends(get_uow_factory)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> BookingService:
    """Build the booking service with its production dependencies.

    Args:
        uow_factory: Shared UnitOfWorkFactory.
        settings: Application settings.
        clock: Injected clock port.

    Returns:
        Ready-to-use BookingService.
    """
    return BookingService(
        uow_factory=uow_factory,
        clock=clock,
        settings=settings,
    )


BookingServiceDependency = Annotated[BookingService, Depends(get_booking_service)]


def get_conversation_service(
    uow_factory: Annotated[UnitOfWorkFactory, Depends(get_uow_factory)],
    booking_service: Annotated[BookingService, Depends(get_booking_service)],
    intent_parser: Annotated[IntentParser, Depends(get_intent_parser)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ConversationService:
    """Build the ConversationService with its production dependencies.

    Args:
        uow_factory: Shared UnitOfWorkFactory.
        booking_service: BookingService domain service.
        intent_parser: Injected IntentParser.
        settings: Application settings.
        clock: Injected clock port.

    Returns:
        Ready-to-use ConversationService.
    """
    return ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=EntityResolver(),
        clock=clock,
        uow_factory=uow_factory,
        settings=settings,
    )


ConversationServiceDependency = Annotated[ConversationService, Depends(get_conversation_service)]
