"""Webhook HMAC-SHA256 signature verification with timestamp tolerance."""

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from booking_agent.api.dependencies import get_app_settings, get_clock
from booking_agent.config import Settings
from booking_agent.domain.ports import Clock


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Compute the HMAC-SHA256 signature for a raw payload and timestamp.

    Args:
        secret: Shared secret string.
        timestamp: Unix epoch timestamp in seconds.
        body: Raw request body bytes.

    Returns:
        Hex-encoded signature string (without the 'sha256=' prefix).
    """
    canonical_payload = f"{timestamp}.".encode() + body
    return hmac.new(
        key=secret.encode(),
        msg=canonical_payload,
        digestmod=hashlib.sha256,
    ).hexdigest()


async def verify_webhook_signature(
    request: Request,
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
    x_timestamp: Annotated[str | None, Header(alias="X-Timestamp")] = None,
) -> bytes:
    """Verify HMAC-SHA256 signature and timestamp tolerance window on incoming webhooks.

    Args:
        request: FastAPI HTTP request.
        clock: Injected clock port.
        settings: Application settings containing webhook_secret and tolerance window.
        x_signature: Hex HMAC digest prefixed with 'sha256='.
        x_timestamp: Unix epoch seconds string.

    Returns:
        Raw request body bytes after cryptographic verification.

    Raises:
        HTTPException: 401 Unauthorized if headers are missing, timestamp is out of
            window, or signature does not match in constant time.
    """
    if not x_signature or not x_timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature or timestamp",
        )

    # Validate header format: sha256=<hex_digest>
    prefix = "sha256="
    if not x_signature.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature or timestamp",
        )
    provided_digest = x_signature[len(prefix) :].strip()

    # Validate timestamp format
    try:
        req_timestamp = int(x_timestamp.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature or timestamp",
        ) from None

    # Check timestamp freshness window (protects against past replays and future drift)
    now_epoch = int(clock.now().timestamp())
    tolerance = settings.webhook_timestamp_tolerance_seconds
    if abs(now_epoch - req_timestamp) > tolerance:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature or timestamp",
        )

    # Read raw body bytes
    raw_body = await request.body()

    # Cryptographically verify the body using constant-time comparison
    secret = settings.webhook_secret.get_secret_value()
    expected_digest = compute_signature(secret=secret, timestamp=req_timestamp, body=raw_body)

    if not hmac.compare_digest(expected_digest, provided_digest):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature or timestamp",
        )

    return raw_body
