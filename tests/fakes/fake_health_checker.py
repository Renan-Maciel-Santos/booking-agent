"""Configurable DatabaseHealthChecker double."""

# Used to simulate a slow database.
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
        # Exception to raise, simulating an outage.
        self._error = error
        # Latency to simulate, used to exercise timeouts.
        self._delay_seconds = delay_seconds
        # Number of times ping() was called.
        self.ping_calls = 0

    async def ping(self) -> None:
        """Simulate a database ping."""
        # Count the invocation.
        self.ping_calls += 1
        # Wait to simulate latency when configured.
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        # Raise the configured failure, if any.
        if self._error is not None:
            raise self._error
