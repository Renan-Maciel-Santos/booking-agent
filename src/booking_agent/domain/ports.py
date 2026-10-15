"""Ports: contracts the domain needs, implemented by adapters in integrations/."""

# Protocol enables structural typing (no inheritance required from adapters).
from typing import Protocol

# Structured result every parser must return.
from booking_agent.domain.booking_intent import BookingIntent


class IntentParser(Protocol):
    """Turns a raw user message into a validated BookingIntent.

    Implementations (e.g. an OpenAI-backed adapter) live in
    booking_agent.integrations.llm. The domain owns this contract so the
    dependency points inwards (Dependency Inversion Principle).
    """

    async def parse(self, user_message: str) -> BookingIntent:
        """Extract intent and entities from a user message.

        Args:
            user_message: Raw text sent by the end user.

        Returns:
            Validated booking intent.
        """
        ...
