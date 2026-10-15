"""Configurable double for the DatabaseHealthChecker port."""

import asyncio


class FakeHealthChecker:
    """Simulates a database that is up, down, or slow."""

    def __init__(
        self,
        error: BaseException | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        """Configure the simulated behaviour.

        Args:
            error: Exception to raise from ping(), if any.
            delay_seconds: Artificial latency before answering.
        """
        self._error = error
        self._delay_seconds = delay_seconds
        self.ping_calls = 0

    async def ping(self) -> None:
        """Simulate a database ping."""
        self.ping_calls += 1
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
