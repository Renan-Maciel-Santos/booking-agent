"""Application factory and lifespan management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from openai import AsyncOpenAI

from booking_agent.api.routers import health
from booking_agent.config import Settings, get_settings
from booking_agent.database.engine import build_engine
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.integrations.llm.openai_intent_parser import OpenAIIntentParser
from booking_agent.integrations.system_clock import SystemClock
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
        """Own the engine and adapter lifecycles: create on startup, dispose on shutdown."""
        app.state.engine = build_engine(app_settings)

        # Wire IntentParser singleton based on configured provider
        if app_settings.llm_provider == "fake":
            app.state.intent_parser = FakeIntentParser(
                clock=SystemClock(),
                business_timezone=ZoneInfo(app_settings.business_timezone),
            )
        else:
            if app_settings.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER is 'openai'")
            client = AsyncOpenAI(
                api_key=app_settings.openai_api_key.get_secret_value(),
                timeout=app_settings.llm_timeout_seconds,
                max_retries=app_settings.llm_max_retries,
            )
            app.state.intent_parser = OpenAIIntentParser(
                client=client,
                model=app_settings.llm_model,
                clock=SystemClock(),
                business_timezone=ZoneInfo(app_settings.business_timezone),
                temperature=app_settings.llm_temperature,
                timeout=app_settings.llm_timeout_seconds,
            )

        logger.info("application started", extra={"environment": app_settings.app_env})
        try:
            yield
        finally:
            if hasattr(app.state, "intent_parser") and isinstance(
                app.state.intent_parser, OpenAIIntentParser
            ):
                await app.state.intent_parser._client.close()
            # Release pooled connections so the database is not left with idle sessions.
            await app.state.engine.dispose()
            logger.info("application stopped")

    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.include_router(health.router)
    return app
