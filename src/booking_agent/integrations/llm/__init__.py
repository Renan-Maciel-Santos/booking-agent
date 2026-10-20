"""LLM adapters implementing booking_agent.domain.ports.IntentParser."""

from booking_agent.integrations.llm.openai_intent_parser import (
    PROMPT_VERSION,
    OpenAIIntentParser,
)

__all__ = ["PROMPT_VERSION", "OpenAIIntentParser"]
