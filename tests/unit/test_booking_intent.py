"""Unit tests for the BookingIntent structured-output contract."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.ports import IntentParser
from tests.fakes.fake_intent_parser import FakeIntentParser


def test_defaults_to_unknown_intent() -> None:
    # Act: build an intent with no data.
    intent = BookingIntent()

    # Assert the safe default.
    assert intent.intent is IntentType.UNKNOWN


def test_accepts_timezone_aware_datetime() -> None:
    # Arrange a timezone-aware slot.
    requested_start = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)

    # Act: parse a typical LLM payload.
    intent = BookingIntent.model_validate(
        {"intent": "book", "service_name": "Haircut", "requested_start": requested_start}
    )

    # Assert values are preserved.
    assert intent.intent is IntentType.BOOK
    assert intent.requested_start == requested_start


def test_rejects_naive_datetime() -> None:
    # Assert naive datetimes are refused to prevent ambiguous bookings.
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "book", "requested_start": "2026-11-03T14:30:00"})


def test_rejects_unexpected_fields_from_the_llm() -> None:
    # Assert extra keys (e.g. an LLM inventing "price") are rejected, not ignored.
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "cancel", "price": 10})


def test_rejects_unknown_intent_value() -> None:
    # Assert only enumerated intents are accepted.
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "delete_everything"})


async def test_fake_parser_satisfies_the_port() -> None:
    # Arrange a canned cancel intent.
    appointment_id = uuid4()
    canned = BookingIntent(intent=IntentType.CANCEL, appointment_id=appointment_id)
    # Type the fake as the port so mypy verifies structural compatibility.
    parser: IntentParser = FakeIntentParser({"cancel my booking": canned})

    # Act: parse a known and an unknown message.
    known = await parser.parse("cancel my booking")
    unknown = await parser.parse("hello")

    # Assert canned and fallback behaviour.
    assert known == canned
    assert unknown.intent is IntentType.UNKNOWN
