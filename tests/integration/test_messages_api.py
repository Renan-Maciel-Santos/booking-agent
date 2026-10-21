"""Integration tests for POST /v1/messages with real PostgreSQL."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from booking_agent.api.dependencies import get_intent_parser
from booking_agent.api.rate_limiter import PhoneRateLimiter
from booking_agent.api.security import compute_signature
from booking_agent.config import Settings
from booking_agent.database.models import ProcessedMessage
from booking_agent.domain.enums import MessageStatus
from booking_agent.domain.errors import LLMUnavailableError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.main import create_app
from tests.fakes.fake_clock import FakeClock


@pytest.fixture
def app_with_postgres(
    integration_settings: Settings,
    engine: AsyncEngine,
) -> FastAPI:
    """Configure FastAPI application connected to real PostgreSQL test container."""
    clock = FakeClock(datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC))
    app = create_app(integration_settings)
    app.state.engine = engine
    app.state.clock = clock
    app.state.rate_limiter = PhoneRateLimiter(
        clock=clock,
        max_requests=integration_settings.rate_limit_requests_per_minute,
        window_seconds=60.0,
    )
    app.state.intent_parser = FakeIntentParser(
        clock=clock,
        business_timezone=ZoneInfo(integration_settings.business_timezone),
    )
    return app


def _sign(
    body: bytes,
    secret: str,
    clock: FakeClock,
) -> dict[str, str]:
    """Generate valid webhook headers for testing."""
    ts = int(clock.now().timestamp())
    sig = compute_signature(secret, ts, body)
    return {
        "Content-Type": "application/json",
        "X-Signature": f"sha256={sig}",
        "X-Timestamp": str(ts),
    }


async def test_duplicate_completed_message_returns_cached_text(
    app_with_postgres: FastAPI,
    integration_settings: Settings,
) -> None:
    # Arrange
    clock: FakeClock = app_with_postgres.state.clock
    secret = integration_settings.webhook_secret.get_secret_value()
    message_id = "int-msg-completed-1"
    payload = {
        "message_id": message_id,
        "phone": "+5511999990001",
        "text": "Olá!",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, secret, clock)

    transport = ASGITransport(app=app_with_postgres)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Act 1: Send initial message
        resp1 = await client.post("/v1/messages", content=raw_body, headers=headers)

        # Assert 1: Successfully processed
        assert resp1.status_code == status.HTTP_200_OK
        first_reply = resp1.json()["response_text"]
        assert first_reply is not None
        assert len(first_reply) > 0

        # Act 2: Send duplicate completed message with identical message_id
        resp2 = await client.post("/v1/messages", content=raw_body, headers=headers)

        # Assert 2: Returns 200 with identical cached text
        assert resp2.status_code == status.HTTP_200_OK
        assert resp2.json()["response_text"] == first_reply


async def test_duplicate_in_progress_message_returns_202_with_null_text(
    app_with_postgres: FastAPI,
    integration_settings: Settings,
    session: AsyncSession,
) -> None:
    # Arrange: Simulate a message lease acquired by another concurrent worker in progress
    clock: FakeClock = app_with_postgres.state.clock
    secret = integration_settings.webhook_secret.get_secret_value()
    message_id = "int-msg-in-progress-1"
    phone = "+5511999990002"

    active_lease = ProcessedMessage(
        message_id=message_id,
        status=MessageStatus.PROCESSING.value,
        response_text=None,
    )
    session.add(active_lease)
    await session.commit()

    payload = {
        "message_id": message_id,
        "phone": phone,
        "text": "Sim",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, secret, clock)

    transport = ASGITransport(app=app_with_postgres)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Act: Duplicate arrives while lease is still actively processing
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert: HTTP 202 Accepted, response_text is null (n8n sends nothing to user)
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json() == {"response_text": None}


async def test_llm_failure_returns_503_marks_failed_and_recovers_on_retry(
    app_with_postgres: FastAPI,
    integration_settings: Settings,
    session: AsyncSession,
) -> None:
    # Arrange: Mock IntentParser to simulate external LLM outage
    clock: FakeClock = app_with_postgres.state.clock
    secret = integration_settings.webhook_secret.get_secret_value()
    message_id = "int-msg-llm-outage-1"
    phone = "+5511999990003"

    failing_parser = AsyncMock()
    failing_parser.parse.side_effect = LLMUnavailableError("OpenAI connection timeout")

    app_with_postgres.dependency_overrides[get_intent_parser] = lambda: failing_parser

    payload = {
        "message_id": message_id,
        "phone": phone,
        "text": "Quero cortar o cabelo amanhã",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, secret, clock)

    transport = ASGITransport(app=app_with_postgres)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Act 1: Initial call encounters LLM failure
        resp1 = await client.post("/v1/messages", content=raw_body, headers=headers)

        # Assert 1: HTTP 503 Service Unavailable returned
        assert resp1.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

        # Verify DB: Message status is recorded as failed (lease released)
        result = await session.execute(
            select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
        )
        msg_record = result.scalar_one_or_none()
        assert msg_record is not None
        assert msg_record.status == MessageStatus.FAILED.value

        # Act 2: LLM service recovers; retry with the EXACT SAME message_id
        app_with_postgres.dependency_overrides.pop(get_intent_parser, None)

        resp2 = await client.post("/v1/messages", content=raw_body, headers=headers)

        # Assert 2: Retried message acquires lease and completes successfully with 200 OK
        assert resp2.status_code == status.HTTP_200_OK
        assert resp2.json()["response_text"] is not None

        # Verify DB: Message status is now done
        session.expire_all()
        result_after = await session.execute(
            select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
        )
        updated_record = result_after.scalar_one_or_none()
        assert updated_record is not None
        assert updated_record.status == MessageStatus.DONE.value
