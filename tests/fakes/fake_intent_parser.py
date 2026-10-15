"""Deterministic IntentParser used in tests instead of a real LLM."""

# Result type the port must return.
from booking_agent.domain.booking_intent import BookingIntent, IntentType


class FakeIntentParser:
    """Returns pre-programmed intents keyed by the exact user message.

    Structurally satisfies booking_agent.domain.ports.IntentParser.
    """

    def __init__(self, responses: dict[str, BookingIntent] | None = None) -> None:
        """Configure canned responses.

        Args:
            responses: Mapping of user message to the intent to return.
        """
        # Copy so the caller's dict cannot be mutated through the fake.
        self._responses = dict(responses or {})
        # Record every message received, for assertions in tests.
        self.received_messages: list[str] = []

    async def parse(self, user_message: str) -> BookingIntent:
        """Return the canned intent, or UNKNOWN for unexpected messages.

        Args:
            user_message: Raw text sent by the end user.

        Returns:
            Configured intent or an UNKNOWN intent.
        """
        # Track the call for later verification.
        self.received_messages.append(user_message)
        # Mirror the real contract's safe fallback for unrecognized input.
        return self._responses.get(user_message, BookingIntent(intent=IntentType.UNKNOWN))
