"""Ports: contracts the inner layers need, implemented by outer layers.

Services depend on these Protocols, never on concrete adapters or repositories,
so dependencies always point inwards (Dependency Inversion Principle).
"""

from datetime import datetime
from typing import Protocol

from booking_agent.domain.booking_intent import BookingIntent


class IntentParser(Protocol):
    """Turns a raw user message into a validated BookingIntent.

    Implementations (e.g. an OpenAI-backed adapter) live in
    booking_agent.integrations.llm.
    """

    async def parse(self, user_message: str) -> BookingIntent:
        """Extract intent and entities from a user message.

        Args:
            user_message: Raw text sent by the end user.

        Returns:
            Validated booking intent.
        """
        ...


class DatabaseHealthChecker(Protocol):
    """Verifies that the database can be reached.

    Implemented by booking_agent.repositories.health_repository.HealthRepository.
    """

    async def ping(self) -> None:
        """Raise if the database cannot be reached."""
        ...


class Clock(Protocol):
    """Port for time-dependent operations.

    Implemented by booking_agent.integrations.system_clock.SystemClock in production,
    and by test doubles (e.g. FakeClock) in tests.
    """

    def now(self) -> datetime:
        """Return the current timezone-aware datetime in UTC."""
        ...
