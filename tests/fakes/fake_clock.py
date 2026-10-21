"""Configurable double for the Clock port."""

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Simulates current time for deterministic testing."""

    def __init__(self, current_time: datetime | None = None) -> None:
        """Initialize with a fixed or default datetime in UTC.

        Args:
            current_time: Simulated current time. Defaults to 2026-10-19 12:00:00 UTC.
        """
        self._current_time: datetime = (
            current_time if current_time is not None else datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
        )

    def now(self) -> datetime:
        """Return the configured current time."""
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        """Advance or change current simulated time.

        Args:
            new_time: New simulated current time.
        """
        self._current_time = new_time

    def advance(self, delta: timedelta) -> None:
        """Advance current simulated time by a duration.

        Args:
            delta: Duration to advance.
        """
        self._current_time += delta
