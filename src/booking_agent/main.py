"""Application factory and lifespan management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from booking_agent.api.routers import health
from booking_agent.config import Settings, get_settings
from booking_agent.database.engine import build_engine
from booking_agent.logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Args:
        settings: Optional settings override (used by tests); defaults to the environment.

    Returns:
        FastAPI application with routers and lifespan wired.
    """
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the engine lifecycle: create on startup, dispose on shutdown."""
        app.state.engine = build_engine(app_settings)
        logger.info("application started", extra={"environment": app_settings.app_env})
        try:
            yield
        finally:
            # Release pooled connections so the database is not left with idle sessions.
            await app.state.engine.dispose()
            logger.info("application stopped")

    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.include_router(health.router)
    return app
