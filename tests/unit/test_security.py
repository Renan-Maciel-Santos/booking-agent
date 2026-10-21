"""Unit tests for webhook HMAC-SHA256 signature verification."""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from booking_agent.api.security import compute_signature, verify_webhook_signature
from booking_agent.config import Settings
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock


@pytest.fixture
def auth_settings() -> Settings:
    """Settings configured with an explicit webhook secret and 300s window."""
    return build_test_settings(webhook_timestamp_tolerance_seconds=300)


@pytest.fixture
def fixed_now() -> datetime:
    """Fixed reference time in UTC."""
    return datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def secure_app(auth_settings: Settings, fixed_now: datetime) -> FastAPI:
    """FastAPI test app with the verify_webhook_signature dependency on a test route."""
    app = FastAPI()
    app.state.settings = auth_settings
    app.state.clock = FakeClock(fixed_now)

    @app.post("/test-webhook")
    async def webhook_endpoint(
        raw_body: bytes = Depends(verify_webhook_signature),
    ) -> JSONResponse:
        return JSONResponse({"status": "verified", "bytes_len": len(raw_body)})

    return app


@pytest.mark.asyncio
async def test_valid_signature_succeeds(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    now_epoch = int(fixed_now.timestamp())
    body = b'{"message_id": "m1", "text": "hello"}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, now_epoch, body)

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=body,
            headers={
                "X-Signature": f"sha256={sig}",
                "X-Timestamp": str(now_epoch),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "verified", "bytes_len": len(body)}


@pytest.mark.asyncio
async def test_missing_signature_header_returns_401(
    secure_app: FastAPI,
    fixed_now: datetime,
) -> None:
    now_epoch = int(fixed_now.timestamp())
    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=b"{}",
            headers={"X-Timestamp": str(now_epoch)},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature or timestamp"}


@pytest.mark.asyncio
async def test_missing_timestamp_header_returns_401(
    secure_app: FastAPI,
) -> None:
    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=b"{}",
            headers={"X-Signature": "sha256=abcdef1234567890"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature or timestamp"}


@pytest.mark.asyncio
async def test_signature_without_sha256_prefix_returns_401(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    now_epoch = int(fixed_now.timestamp())
    body = b'{"test": 1}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, now_epoch, body)

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=body,
            headers={
                "X-Signature": sig,  # missing sha256= prefix
                "X-Timestamp": str(now_epoch),
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_signature_digest_returns_401(
    secure_app: FastAPI,
    fixed_now: datetime,
) -> None:
    now_epoch = int(fixed_now.timestamp())
    fake_digest = "0" * 64
    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=b'{"test": 1}',
            headers={
                "X-Signature": f"sha256={fake_digest}",
                "X-Timestamp": str(now_epoch),
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature or timestamp"}


@pytest.mark.asyncio
async def test_body_tampered_after_signing_returns_401(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    now_epoch = int(fixed_now.timestamp())
    original_body = b'{"amount": 100}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, now_epoch, original_body)

    # Tampered body sent to server
    tampered_body = b'{"amount": 999}'

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=tampered_body,
            headers={
                "X-Signature": f"sha256={sig}",
                "X-Timestamp": str(now_epoch),
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature or timestamp"}


@pytest.mark.asyncio
async def test_timestamp_expired_in_past_returns_401(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    # 301 seconds in the past (> 300s tolerance)
    expired_epoch = int(fixed_now.timestamp()) - 301
    body = b'{"test": "replay"}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, expired_epoch, body)

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=body,
            headers={
                "X-Signature": f"sha256={sig}",
                "X-Timestamp": str(expired_epoch),
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature or timestamp"}


@pytest.mark.asyncio
async def test_timestamp_too_far_in_future_returns_401(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    # 305 seconds in the future
    future_epoch = int(fixed_now.timestamp()) + 305
    body = b'{"test": "future"}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, future_epoch, body)

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=body,
            headers={
                "X-Signature": f"sha256={sig}",
                "X-Timestamp": str(future_epoch),
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_timestamp_within_tolerance_window_succeeds(
    secure_app: FastAPI,
    auth_settings: Settings,
    fixed_now: datetime,
) -> None:
    # 60 seconds in the past (< 300s tolerance)
    valid_past_epoch = int(fixed_now.timestamp()) - 60
    body = b'{"test": "valid_window"}'
    secret = auth_settings.webhook_secret.get_secret_value()
    sig = compute_signature(secret, valid_past_epoch, body)

    transport = ASGITransport(app=secure_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/test-webhook",
            content=body,
            headers={
                "X-Signature": f"sha256={sig}",
                "X-Timestamp": str(valid_past_epoch),
            },
        )

    assert response.status_code == 200
