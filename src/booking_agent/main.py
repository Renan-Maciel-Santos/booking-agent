"""Application factory and lifespan management."""

# Logger for startup/shutdown events.
import logging

# Types for the async lifespan generator.
from collections.abc import AsyncIterator

# Turns a generator into an async context manager.
from contextlib import asynccontextmanager

# The ASGI application class.
from fastapi import FastAPI

# Routers mounted by the factory.
from booking_agent.api.routers import health

# Settings provider.
from booking_agent.config import Settings, get_settings

# Engine factory.
from booking_agent.database.engine import build_engine

# Logging bootstrap.
from booking_agent.logging_config import configure_logging

# Module-scoped logger.
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Args:
        settings: Optional settings override (used by tests); defaults to the environment.

    Returns:
        FastAPI application with routers and lifespan wired.
    """
    # Fall back to environment-driven settings when none are injected.
    app_settings = settings or get_settings()
    # Configure logging before anything else emits records.
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the engine lifecycle: create on startup, dispose on shutdown."""
        # One pooled engine per application instance.
        app.state.engine = build_engine(app_settings)
        # Record startup with context for observability.
        logger.info("application started", extra={"environment": app_settings.app_env})
        try:
            # Hand control to the server while the app serves requests.
            yield
        finally:
            # Close pooled connections so the database is not left with idle sessions.
            await app.state.engine.dispose()
            # Record graceful shutdown.
            logger.info("application stopped")

    # Instantiate the app with metadata used by the OpenAPI docs.
    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    # Expose the settings to request-scoped dependencies.
    app.state.settings = app_settings
    # Mount the health routes.
    app.include_router(health.router)
    # Return the fully wired application.
    return app
