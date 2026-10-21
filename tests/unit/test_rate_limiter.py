"""Unit tests for PhoneRateLimiter with injected clock."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from booking_agent.api.rate_limiter import PhoneRateLimiter, RateLimiterDependency
from tests.conftest import build_test_settings
from tests.fakes.fake_clock import FakeClock


@pytest.fixture
def fake_clock() -> FakeClock:
    """Fixed initial reference clock."""
    return FakeClock(datetime(2026, 10, 21, 14, 0, 0, tzinfo=UTC))


@pytest.fixture
def rate_limiter(fake_clock: FakeClock) -> PhoneRateLimiter:
    """Rate limiter configured for 3 requests per 60 seconds."""
    return PhoneRateLimiter(clock=fake_clock, max_requests=3, window_seconds=60.0)


@pytest.mark.asyncio
async def test_allows_requests_up_to_limit(rate_limiter: PhoneRateLimiter) -> None:
    phone = "+5511999990001"

    assert await rate_limiter.is_allowed(phone) is True
    assert await rate_limiter.is_allowed(phone) is True
    assert await rate_limiter.is_allowed(phone) is True

    # 4th request exceeds max_requests=3
    assert await rate_limiter.is_allowed(phone) is False


@pytest.mark.asyncio
async def test_independent_quotas_per_phone(rate_limiter: PhoneRateLimiter) -> None:
    phone_a = "+5511999990001"
    phone_b = "+5511999990002"

    for _ in range(3):
        assert await rate_limiter.is_allowed(phone_a) is True

    # Phone A is blocked
    assert await rate_limiter.is_allowed(phone_a) is False

    # Phone B is untouched and allowed
    assert await rate_limiter.is_allowed(phone_b) is True


@pytest.mark.asyncio
async def test_quota_restored_when_clock_advances_without_real_sleep(
    rate_limiter: PhoneRateLimiter,
    fake_clock: FakeClock,
) -> None:
    phone = "+5511999990001"

    # Exhaust quota at t=0
    for _ in range(3):
        assert await rate_limiter.is_allowed(phone) is True
    assert await rate_limiter.is_allowed(phone) is False

    # Advance clock by 61 seconds (beyond the 60s sliding window)
    fake_clock.advance(timedelta(seconds=61))

    # Quota is restored immediately without waiting real time
    assert await rate_limiter.is_allowed(phone) is True


@pytest.mark.asyncio
async def test_check_raises_429_when_exceeded(rate_limiter: PhoneRateLimiter) -> None:
    phone = "+5511999990001"

    for _ in range(3):
        await rate_limiter.check(phone)

    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check(phone)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "Too many requests. Please try again later."


@pytest.mark.asyncio
async def test_prune_cleans_stale_records(
    rate_limiter: PhoneRateLimiter,
    fake_clock: FakeClock,
) -> None:
    phone = "+5511999990001"
    await rate_limiter.is_allowed(phone)
    assert phone in rate_limiter._requests

    # Advance past window
    fake_clock.advance(timedelta(seconds=65))
    await rate_limiter.prune()

    assert phone not in rate_limiter._requests


@pytest.mark.asyncio
async def test_inactive_phones_evicted_from_memory_automatically(
    fake_clock: FakeClock,
) -> None:
    # Arrange: limiter configured to sweep every 2 calls
    limiter = PhoneRateLimiter(
        clock=fake_clock,
        max_requests=5,
        window_seconds=60.0,
        cleanup_interval=2,
    )
    phone_a = "+5511999990001"
    phone_b = "+5511999990002"

    # Phone A makes a call at t=0
    assert await limiter.is_allowed(phone_a) is True
    assert phone_a in limiter._requests

    # Advance clock past the rolling window (inactive phone A)
    fake_clock.advance(timedelta(seconds=65))

    # Phone B makes 2 calls at t=65, reaching cleanup_interval=2 and triggering periodic sweep
    assert await limiter.is_allowed(phone_b) is True
    assert await limiter.is_allowed(phone_b) is True

    # Assert: Phone A was automatically evicted from memory without explicit prune() call
    assert phone_a not in limiter._requests
    assert phone_b in limiter._requests


@pytest.mark.asyncio
async def test_reentering_phone_cleans_stale_timestamps_before_adding_new(
    fake_clock: FakeClock,
) -> None:
    # Arrange
    limiter = PhoneRateLimiter(clock=fake_clock, max_requests=5, window_seconds=60.0)
    phone = "+5511999990001"

    assert await limiter.is_allowed(phone) is True
    t0 = fake_clock.now().timestamp()
    assert limiter._requests[phone] == [t0]

    # Advance past window
    fake_clock.advance(timedelta(seconds=80))
    t1 = fake_clock.now().timestamp()

    # Re-entry: old timestamps cleared, only new timestamp kept
    assert await limiter.is_allowed(phone) is True
    assert limiter._requests[phone] == [t1]


@pytest.mark.asyncio
async def test_get_rate_limiter_dependency_in_fastapi(
    fake_clock: FakeClock,
) -> None:
    settings = build_test_settings(rate_limit_requests_per_minute=2)
    app = FastAPI()
    app.state.settings = settings
    app.state.clock = fake_clock

    limiter = PhoneRateLimiter(clock=fake_clock, max_requests=2, window_seconds=60.0)
    app.state.rate_limiter = limiter

    @app.post("/test-limit/{phone}")
    async def limited_endpoint(
        phone: str,
        limiter_dep: RateLimiterDependency,
    ) -> JSONResponse:
        await limiter_dep.check(phone)
        return JSONResponse({"status": "ok"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post("/test-limit/+5511999991111")
        assert r1.status_code == 200

        r2 = await client.post("/test-limit/+5511999991111")
        assert r2.status_code == 200

        r3 = await client.post("/test-limit/+5511999991111")
        assert r3.status_code == 429
        assert r3.json() == {"detail": "Too many requests. Please try again later."}
