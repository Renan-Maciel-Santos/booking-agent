"""System clock adapter implementing domain.ports.Clock."""

from datetime import UTC, datetime


class SystemClock:
    """Default system clock providing current UTC datetime."""

    def now(self) -> datetime:
        """Return the current system time in UTC."""
        return datetime.now(UTC)
