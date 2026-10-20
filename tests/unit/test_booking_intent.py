"""Unit tests for the BookingIntent structured-output contract."""

from datetime import date, time

import pytest
from pydantic import ValidationError

from booking_agent.domain.booking_intent import BookingIntent, IntentType, TimePreference
from booking_agent.domain.ports import IntentParser
from tests.fakes.fake_intent_parser import FakeIntentParser


def test_defaults_to_unknown_intent() -> None:
    # Act: build an intent with no data.
    intent = BookingIntent()

    # Assert safe defaults.
    assert intent.intent is IntentType.UNKNOWN
    assert intent.service_name is None
    assert intent.local_date is None
    assert intent.local_time is None
    assert intent.time_preference is None
    assert intent.barber_name is None
    assert intent.customer_name is None


def test_accepts_valid_booking_payload() -> None:
    target_date = date(2026, 11, 3)
    target_time = time(14, 30)

    intent = BookingIntent.model_validate(
        {
            "intent": "book",
            "service_name": "Haircut",
            "local_date": "2026-11-03",
            "local_time": "14:30:00",
            "time_preference": "afternoon",
            "barber_name": "Carlos",
            "customer_name": "Marcos",
        }
    )

    assert intent.intent is IntentType.BOOK
    assert intent.service_name == "Haircut"
    assert intent.local_date == target_date
    assert intent.local_time == target_time
    assert intent.time_preference is TimePreference.AFTERNOON
    assert intent.barber_name == "Carlos"
    assert intent.customer_name == "Marcos"


def test_rejects_unexpected_fields_from_the_llm() -> None:
    # Assert extra keys (e.g. removed appointment_id or requested_start) are rejected.
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "cancel", "price": 10})

    with pytest.raises(ValidationError):
        BookingIntent.model_validate(
            {"intent": "cancel", "appointment_id": "00000000-0000-0000-0000-000000000000"}
        )

    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "book", "requested_start": "2026-11-03T14:30:00Z"})


def test_rejects_unknown_intent_value() -> None:
    # Assert only enumerated intents are accepted.
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "delete_everything"})


def test_rejects_invalid_time_preference() -> None:
    with pytest.raises(ValidationError):
        BookingIntent.model_validate({"intent": "book", "time_preference": "midnight"})


async def test_fake_parser_satisfies_the_port() -> None:
    # Arrange a canned cancel intent.
    canned = BookingIntent(intent=IntentType.CANCEL, customer_name="Marcos")
    # Type the fake as the port so mypy verifies structural compatibility.
    parser: IntentParser = FakeIntentParser({"cancel my booking": canned})

    # Act: parse a known and an unknown message.
    known = await parser.parse("cancel my booking")
    unknown = await parser.parse("hello")

    # Assert canned and fallback behaviour.
    assert known == canned
    assert unknown.intent is IntentType.UNKNOWN
