"""Unit tests for POST /v1/messages endpoint, security, validation, and telemetry."""

import json
import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from booking_agent.api.dependencies import (
    get_clock,
    get_conversation_service,
    get_uow_factory,
)
from booking_agent.api.rate_limiter import PhoneRateLimiter, get_rate_limiter
from booking_agent.api.security import compute_signature
from booking_agent.config import Settings
from booking_agent.database.models import ProcessedMessage
from booking_agent.domain.enums import MessageStatus
from booking_agent.domain.errors import LLMConfigurationError, LLMUnavailableError
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from booking_agent.main import create_app
from booking_agent.services.booking_service import BookingService
from booking_agent.services.conversation_service import ConversationService, TurnResult
from booking_agent.services.entity_resolver import EntityResolver
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_uow import FakeUnitOfWorkFactory, InMemoryDataStore


def _build_test_client(
    settings: Settings,
    clock: FakeClock,
    turn_result: TurnResult | None = None,
    conversation_service_override: ConversationService | AsyncMock | None = None,
    rate_limiter_override: PhoneRateLimiter | None = None,
    uow_factory_override: FakeUnitOfWorkFactory | None = None,
) -> AsyncClient:
    """Create an AsyncClient with mocked/faked dependencies for unit isolation."""
    app = create_app(settings)
    app.state.rate_limiter = rate_limiter_override or PhoneRateLimiter(
        clock=clock,
        max_requests=settings.rate_limit_requests_per_minute,
        window_seconds=60.0,
    )
    uow_factory = uow_factory_override or FakeUnitOfWorkFactory(clock=clock)

    if conversation_service_override is not None:
        service_mock = conversation_service_override
    else:
        service_mock = AsyncMock(spec=ConversationService)
        service_mock.process_turn.return_value = turn_result or TurnResult(
            response_text="Agendamento confirmado!",
            status_code=200,
            outcome="booking_confirmed",
            fast_path=False,
            stage_from="AWAITING_CONFIRMATION",
            stage_to="IDLE",
            phase1_lease_ms=1.5,
            phase2_llm_ms=25.0,
            phase3_state_ms=2.0,
            phase4_action_ms=5.0,
            total_duration_ms=33.5,
        )

    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_uow_factory] = lambda: uow_factory
    app.dependency_overrides[get_conversation_service] = lambda: service_mock

    if rate_limiter_override is not None:
        app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter_override

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _sign(
    body: bytes,
    secret: str,
    timestamp: str | None = None,
    fake_clock: FakeClock | None = None,
) -> dict[str, str]:
    """Generate canonical headers for webhook testing."""
    ts = (
        timestamp
        if timestamp is not None
        else str(int((fake_clock or FakeClock()).now().timestamp()))
    )
    sig = compute_signature(secret, int(ts), body)
    return {
        "Content-Type": "application/json",
        "X-Signature": f"sha256={sig}",
        "X-Timestamp": ts,
    }


async def test_valid_signature_returns_200(test_settings: Settings) -> None:
    # Arrange
    clock = FakeClock()
    payload = {
        "message_id": "msg-unit-1",
        "phone": "+5511999998888",
        "text": "Olá, gostaria de agendar um corte amanhã às 15h",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, test_settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"response_text": "Agendamento confirmado!"}


async def test_invalid_signature_returns_401(test_settings: Settings) -> None:
    # Arrange
    clock = FakeClock()
    payload = {
        "message_id": "msg-unit-bad-sig",
        "phone": "+5511999998888",
        "text": "Olá",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Signature": "sha256=invalidhexdigest1234567890abcdef1234567890abcdef",
        "X-Timestamp": str(int(clock.now().timestamp())),
    }

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": "Invalid signature or timestamp"}


async def test_missing_signature_headers_returns_401(test_settings: Settings) -> None:
    # Arrange
    clock = FakeClock()
    raw_body = b'{"message_id":"1","phone":"+5511999998888","text":"hi"}'

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post(
            "/v1/messages", content=raw_body, headers={"Content-Type": "application/json"}
        )

    # Assert
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": "Invalid signature or timestamp"}


async def test_expired_timestamp_returns_401(test_settings: Settings) -> None:
    # Arrange: timestamp 600s in the past (tolerance is 300s)
    clock = FakeClock()
    stale_time = clock.now() - timedelta(seconds=600)
    raw_body = b'{"message_id":"msg-stale","phone":"+5511999998888","text":"hi"}'
    headers = _sign(
        raw_body,
        test_settings.webhook_secret.get_secret_value(),
        timestamp=str(int(stale_time.timestamp())),
        fake_clock=clock,
    )

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": "Invalid signature or timestamp"}


async def test_future_timestamp_returns_401(test_settings: Settings) -> None:
    # Arrange: timestamp 600s in the future
    clock = FakeClock()
    future_time = clock.now() + timedelta(seconds=600)
    raw_body = b'{"message_id":"msg-future","phone":"+5511999998888","text":"hi"}'
    headers = _sign(
        raw_body,
        test_settings.webhook_secret.get_secret_value(),
        timestamp=str(int(future_time.timestamp())),
        fake_clock=clock,
    )

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": "Invalid signature or timestamp"}


async def test_tampered_body_after_signed_returns_401(test_settings: Settings) -> None:
    # Arrange: sign legitimate body, but send modified body
    clock = FakeClock()
    original_body = b'{"message_id":"1","phone":"+5511999998888","text":"hi"}'
    secret = test_settings.webhook_secret.get_secret_value()
    headers = _sign(original_body, secret, fake_clock=clock)
    tampered_body = b'{"message_id":"1","phone":"+5511999998888","text":"malicious"}'

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=tampered_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_body_exceeding_max_bytes_returns_413(test_settings: Settings) -> None:
    # Arrange: configure max_body_bytes=100 and send body with 200 bytes
    clock = FakeClock()
    small_limit_settings = test_settings.model_copy(update={"max_body_bytes": 100})
    large_payload = {
        "message_id": "msg-large",
        "phone": "+5511999998888",
        "text": "A" * 150,
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(large_payload).encode("utf-8")
    assert len(raw_body) > 100

    secret = small_limit_settings.webhook_secret.get_secret_value()
    headers = _sign(raw_body, secret, fake_clock=clock)

    # Act
    async with _build_test_client(small_limit_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


async def test_rate_limiter_exceeded_returns_429(test_settings: Settings) -> None:
    # Arrange: rate limit of 2 requests per window
    clock = FakeClock()
    limiter = PhoneRateLimiter(clock=clock, max_requests=2, window_seconds=60.0)

    payload = {
        "message_id": "msg-rl",
        "phone": "+5511999998888",
        "text": "Oi",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, test_settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act & Assert
    async with _build_test_client(test_settings, clock, rate_limiter_override=limiter) as client:
        resp1 = await client.post("/v1/messages", content=raw_body, headers=headers)
        assert resp1.status_code == status.HTTP_200_OK

        resp2 = await client.post("/v1/messages", content=raw_body, headers=headers)
        assert resp2.status_code == status.HTTP_200_OK

        # 3rd request from same phone within same minute must be rejected with 429
        resp3 = await client.post("/v1/messages", content=raw_body, headers=headers)
        assert resp3.status_code == status.HTTP_429_TOO_MANY_REQUESTS


async def test_extra_fields_in_payload_returns_400(test_settings: Settings) -> None:
    # Arrange: extra unrecognized field
    clock = FakeClock()
    payload = {
        "message_id": "msg-extra",
        "phone": "+5511999998888",
        "text": "Oi",
        "timestamp": clock.now().isoformat(),
        "malicious_extra_field": "hacked",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, test_settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "extra_forbidden" in str(response.json()["detail"])


async def test_invalid_phone_format_returns_400(test_settings: Settings) -> None:
    # Arrange: invalid phone not matching E.164
    clock = FakeClock()
    payload = {
        "message_id": "msg-invalid-phone",
        "phone": "invalid_phone_123",
        "text": "Oi",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, test_settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_empty_or_whitespace_text_returns_400(test_settings: Settings) -> None:
    # Arrange: blank text
    clock = FakeClock()
    payload = {
        "message_id": "msg-blank-text",
        "phone": "+5511999998888",
        "text": "   ",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, test_settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_x_request_id_present_in_response_and_logs(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: enable INFO level to capture request execution logs
    clock = FakeClock()
    settings = test_settings.model_copy(update={"log_level": "INFO"})
    payload = {
        "message_id": "msg-req-id",
        "phone": "+5511999998888",
        "text": "Agendar amanhã",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act
    with caplog.at_level(logging.INFO):
        async with _build_test_client(settings, clock) as client:
            response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_200_OK
    request_id_header = response.headers.get("X-Request-ID")
    assert request_id_header is not None
    assert len(request_id_header) == 36
    # Log records within the request context carry the same request_id
    assert any(getattr(r, "request_id", None) == request_id_header for r in caplog.records)


async def test_phone_masked_and_text_absent_from_logs(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: customer message containing sensitive text and phone
    clock = FakeClock()
    settings = test_settings.model_copy(update={"log_level": "INFO"})
    sensitive_text = "Meu segredo confidencial 12345"
    raw_phone = "+5511999998888"
    payload = {
        "message_id": "msg-privacy-test",
        "phone": raw_phone,
        "text": sensitive_text,
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, settings.webhook_secret.get_secret_value(), fake_clock=clock)

    # Act: capture logs across all levels
    with caplog.at_level(logging.DEBUG):
        async with _build_test_client(settings, clock) as client:
            response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_200_OK

    all_logs = caplog.text
    # 1. Message text must NEVER appear in formatted log text
    assert sensitive_text not in all_logs
    # 2. Raw unmasked phone must NEVER appear in formatted log text
    assert raw_phone not in all_logs
    # 3. Masked phone must appear in structured extra attributes
    assert any(getattr(r, "phone_masked", None) == "+5511*****8888" for r in caplog.records)
    # 4. Message text must not exist in any record's attribute dictionary
    assert not any(sensitive_text in str(v) for r in caplog.records for v in vars(r).values())


async def test_llm_configuration_error_returns_503_and_logs_error(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: LLM configuration error (requires operator intervention)
    clock = FakeClock()
    settings = test_settings.model_copy(update={"log_level": "INFO"})
    payload = {
        "message_id": "msg-llm-cfg-err",
        "phone": "+5511999998888",
        "text": "Agendar amanhã",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, settings.webhook_secret.get_secret_value(), fake_clock=clock)

    service_mock = AsyncMock(spec=ConversationService)
    service_mock.process_turn.side_effect = LLMConfigurationError("Invalid model configuration")

    # Act
    with caplog.at_level(logging.INFO):
        async with _build_test_client(
            settings, clock, conversation_service_override=service_mock
        ) as client:
            response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) >= 1
    assert any("LLM configuration error" in r.getMessage() for r in error_records)


async def test_llm_unavailable_error_returns_503_and_logs_warning(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: transient LLM outage (temporary external failure)
    clock = FakeClock()
    settings = test_settings.model_copy(update={"log_level": "INFO"})
    payload = {
        "message_id": "msg-llm-unavail-err",
        "phone": "+5511999998888",
        "text": "Agendar amanhã",
        "timestamp": clock.now().isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(raw_body, settings.webhook_secret.get_secret_value(), fake_clock=clock)

    service_mock = AsyncMock(spec=ConversationService)
    service_mock.process_turn.side_effect = LLMUnavailableError("OpenAI connection timed out")

    # Act
    with caplog.at_level(logging.INFO):
        async with _build_test_client(
            settings, clock, conversation_service_override=service_mock
        ) as client:
            response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) >= 1
    assert any("LLM service failure" in r.getMessage() for r in warning_records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


async def test_malformed_json_without_valid_signature_returns_401(
    test_settings: Settings,
) -> None:
    # Arrange: malformed JSON body sent with missing or invalid signature
    clock = FakeClock()
    malformed_body = b'{"message_id": "bad-json", "phone":'

    # Act: post with invalid signature headers
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post(
            "/v1/messages",
            content=malformed_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": "sha256=invalidsignature1234567890abcdef1234567890abcdef",
                "X-Timestamp": str(int(clock.now().timestamp())),
            },
        )

    # Assert: signature check happens before parsing body; must return 401, NOT 400
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": "Invalid signature or timestamp"}


async def test_malformed_json_with_valid_signature_returns_400(
    test_settings: Settings,
) -> None:
    # Arrange: malformed JSON body sent with cryptographically valid HMAC signature
    clock = FakeClock()
    malformed_body = b'{"message_id": "bad-json", "phone":'
    headers = _sign(
        malformed_body,
        test_settings.webhook_secret.get_secret_value(),
        fake_clock=clock,
    )

    # Act
    async with _build_test_client(test_settings, clock) as client:
        response = await client.post("/v1/messages", content=malformed_body, headers=headers)

    # Assert: signature verification succeeds, but JSON parsing / Pydantic validation fails -> 400
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "detail" in response.json()


def test_n8n_workflow_file_is_valid_json() -> None:
    # Arrange: find workflow JSON artifact
    workflow_path = (
        Path(__file__).parents[2] / "integrations" / "n8n" / "whatsapp_booking_agent_workflow.json"
    )

    # Act
    assert workflow_path.exists(), f"Workflow file missing at {workflow_path}"
    with open(workflow_path, encoding="utf-8") as f:
        data = json.load(f)

    # Assert
    assert isinstance(data, dict)
    assert data["name"] == "WhatsApp Booking Agent Integration"
    node_names = [n["name"] for n in data.get("nodes", [])]
    assert "WhatsApp Webhook Trigger" in node_names
    assert "Normalize & Sign Payload" in node_names
    assert "POST /v1/messages" in node_names
    assert "Switch by HTTP Status" in node_names


async def test_replaying_redacted_done_message_returns_200_with_null_reply_and_no_appointment(
    test_settings: Settings,
) -> None:
    # Arrange: seed an already-completed message whose response_text was redacted to None
    clock = FakeClock()
    store = InMemoryDataStore()
    uow_factory = FakeUnitOfWorkFactory(store=store, clock=clock)

    msg_id = "msg-redacted-done-replay"
    phone = "+5511999998888"
    now = clock.now()
    store.processed_messages[msg_id] = ProcessedMessage(
        message_id=msg_id,
        status=MessageStatus.DONE.value,
        response_text=None,
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
    )

    booking_service = BookingService(uow_factory=uow_factory, clock=clock, settings=test_settings)
    entity_resolver = EntityResolver()
    intent_parser = FakeIntentParser(clock=clock)
    conv_service = ConversationService(
        intent_parser=intent_parser,
        booking_service=booking_service,
        entity_resolver=entity_resolver,
        clock=clock,
        uow_factory=uow_factory,
        settings=test_settings,
    )

    payload = {
        "message_id": msg_id,
        "phone": phone,
        "text": "Quero agendar um corte amanhã às 14h",
        "timestamp": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = _sign(
        raw_body,
        test_settings.webhook_secret.get_secret_value(),
        fake_clock=clock,
    )

    # Act
    async with _build_test_client(
        test_settings,
        clock,
        conversation_service_override=conv_service,
        uow_factory_override=uow_factory,
    ) as client:
        response = await client.post("/v1/messages", content=raw_body, headers=headers)

    # Assert: 200 OK with null response_text (no fabricated greeting fallback)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"response_text": None}
    assert len(store.appointments) == 0
