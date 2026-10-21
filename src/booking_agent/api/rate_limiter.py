"""In-memory sliding-window rate limiter per phone number with injected clock."""

import asyncio
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from booking_agent.domain.ports import Clock


class PhoneRateLimiter:
    """In-memory sliding-window rate limiter partitioned by phone number.

    Tracks request timestamps within a rolling window. Thread/async-safe via
    an asyncio.Lock. Injected Clock enables deterministic testing without sleep.
    """

    def __init__(
        self,
        clock: Clock,
        max_requests: int = 10,
        window_seconds: float = 60.0,
        cleanup_interval: int = 50,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            clock: Injected clock port returning current timezone-aware datetime.
            max_requests: Maximum allowed requests within the window.
            window_seconds: Rolling window duration in seconds.
            cleanup_interval: Run full sweep of stale entries every N calls.
        """
        self._clock = clock
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._cleanup_interval = cleanup_interval
        self._requests: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._call_count: int = 0

    def _prune_stale_locked(self, cutoff: float) -> None:
        """Evict all phone entries with no timestamps within the window (lock must be held)."""
        stale_keys = [
            phone
            for phone, timestamps in self._requests.items()
            if not any(ts > cutoff for ts in timestamps)
        ]
        for key in stale_keys:
            del self._requests[key]

    async def is_allowed(self, phone: str) -> bool:
        """Determine whether an incoming request from phone is within quota.

        Args:
            phone: Caller phone number (E.164 string).

        Returns:
            True if request is allowed, False if rate limit is exceeded.
        """
        now = self._clock.now().timestamp()
        cutoff = now - self._window_seconds

        async with self._lock:
            # Cheap periodic sweep of stale keys across all phones every N calls
            self._call_count += 1
            if self._call_count % self._cleanup_interval == 0:
                self._prune_stale_locked(cutoff)

            timestamps = self._requests.get(phone, [])
            valid_timestamps = [ts for ts in timestamps if ts > cutoff]

            # Immediately remove phone entry if no timestamps remain in the current window
            if phone in self._requests and not valid_timestamps:
                del self._requests[phone]

            if len(valid_timestamps) >= self._max_requests:
                self._requests[phone] = valid_timestamps
                return False

            valid_timestamps.append(now)
            self._requests[phone] = valid_timestamps
            return True

    async def check(self, phone: str) -> None:
        """Check rate limit for phone, raising 429 Too Many Requests if exceeded.

        Args:
            phone: Caller phone number.

        Raises:
            HTTPException: 429 Too Many Requests if quota is exceeded.
        """
        allowed = await self.is_allowed(phone)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )

    async def prune(self) -> None:
        """Remove empty or fully expired phone entries to prevent unbounded memory growth."""
        now = self._clock.now().timestamp()
        cutoff = now - self._window_seconds
        async with self._lock:
            self._prune_stale_locked(cutoff)


def get_rate_limiter(request: Request) -> PhoneRateLimiter:
    """Return the process-wide PhoneRateLimiter instance from app.state.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Singleton PhoneRateLimiter bound to application lifespan.
    """
    limiter: PhoneRateLimiter = request.app.state.rate_limiter
    return limiter


RateLimiterDependency = Annotated[PhoneRateLimiter, Depends(get_rate_limiter)]
