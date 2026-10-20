"""Deterministic IntentParser used in tests instead of a real LLM."""

from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser

__all__ = ["FakeIntentParser"]
