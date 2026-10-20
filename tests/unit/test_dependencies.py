"""Unit tests for FastAPI dependency wiring and application lifespan."""

from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import SecretStr

from booking_agent.api.dependencies import get_intent_parser
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.integrations.llm.openai_intent_parser import OpenAIIntentParser
from booking_agent.main import create_app
from tests.conftest import build_test_settings


def test_get_intent_parser_retrieves_from_app_state() -> None:
    mock_parser = MagicMock()
    request = MagicMock(spec=Request)
    request.app.state.intent_parser = mock_parser

    assert get_intent_parser(request) is mock_parser


async def test_lifespan_initializes_fake_intent_parser() -> None:
    settings = build_test_settings(llm_provider="fake")
    app = create_app(settings)

    # Mock engine build and dispose so lifespan runs in unit test without PostgreSQL
    app.state.engine = MagicMock()
    app.state.engine.dispose = AsyncMock()

    async with app.router.lifespan_context(app):
        assert hasattr(app.state, "intent_parser")
        assert isinstance(app.state.intent_parser, FakeIntentParser)


async def test_lifespan_initializes_openai_intent_parser() -> None:
    settings = build_test_settings(
        llm_provider="openai",
        openai_api_key=SecretStr("sk-test-mock-key"),
        business_timezone="America/Sao_Paulo",
    )
    app = create_app(settings)

    app.state.engine = MagicMock()
    app.state.engine.dispose = AsyncMock()

    async with app.router.lifespan_context(app):
        assert hasattr(app.state, "intent_parser")
        assert isinstance(app.state.intent_parser, OpenAIIntentParser)
        assert app.state.intent_parser._tz == ZoneInfo("America/Sao_Paulo")
