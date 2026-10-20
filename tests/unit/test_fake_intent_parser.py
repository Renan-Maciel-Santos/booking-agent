"""Unit tests for deterministic FakeIntentParser heuristics, relative dates, and warnings."""

import logging
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from booking_agent.domain.booking_intent import BookingIntent, IntentType, TimePreference
from booking_agent.integrations.llm.fake_intent_parser import FakeIntentParser
from tests.fakes.fake_clock import FakeClock

TEST_TZ = ZoneInfo("America/Sao_Paulo")
# 2026-10-20 is a Tuesday
FIXED_NOW_UTC = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def test_fake_parser_logs_warning_on_initialization(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        FakeIntentParser()

    assert any(
        "Running with FakeIntentParser. LLM calls are mocked." in record.message
        for record in caplog.records
    )


async def test_fake_parser_returns_canned_response() -> None:
    canned = BookingIntent(intent=IntentType.BOOK, service_name="custom")
    parser = FakeIntentParser(responses={"exact message": canned})

    result = await parser.parse("exact message")
    assert result == canned
    assert parser.received_messages == ["exact message"]


async def test_fake_parser_parses_booking_with_relative_date_and_barber() -> None:
    clock = FakeClock(FIXED_NOW_UTC)
    parser = FakeIntentParser(clock=clock, business_timezone=TEST_TZ)

    # "amanha" relative to Tuesday 2026-10-20 -> Wednesday 2026-10-21
    intent = await parser.parse(
        "opa, queria agendar um degradê amanha as 15h com o carlos, sou o marcos"
    )

    assert intent.intent is IntentType.BOOK
    assert intent.service_name == "degradê"
    assert intent.local_date == date(2026, 10, 21)
    assert intent.local_time == time(15, 0)
    assert intent.barber_name == "Carlos"
    assert intent.customer_name == "Marcos"


async def test_fake_parser_parses_arbitrary_barber_name_generically() -> None:
    parser = FakeIntentParser()
    intent1 = await parser.parse("quero agendar corte com a beatriz")
    assert intent1.barber_name == "Beatriz"

    intent2 = await parser.parse("agendar barba com o barbeiro rodrigo")
    assert intent2.barber_name == "Rodrigo"

    intent3 = await parser.parse("quero cortar amanhã com qualquer um")
    assert intent3.barber_name is None


async def test_fake_parser_parses_reschedule_with_time_preference() -> None:
    clock = FakeClock(FIXED_NOW_UTC)
    parser = FakeIntentParser(clock=clock, business_timezone=TEST_TZ)

    # Tuesday 2026-10-20 + "depois de amanha" -> Thursday 2026-10-22
    intent = await parser.parse("quero remarcar meu horario para depois de amanhã de manhã")

    assert intent.intent is IntentType.RESCHEDULE
    assert intent.local_date == date(2026, 10, 22)
    assert intent.time_preference is TimePreference.MORNING
    assert intent.local_time is None


async def test_fake_parser_parses_cancellation() -> None:
    parser = FakeIntentParser()
    intent = await parser.parse("por favor, desmarcar meu agendamento")

    assert intent.intent is IntentType.CANCEL


async def test_fake_parser_parses_weekday_relative_date() -> None:
    clock = FakeClock(FIXED_NOW_UTC)  # Tuesday 2026-10-20
    parser = FakeIntentParser(clock=clock, business_timezone=TEST_TZ)

    # Next Friday (weekday index 4) relative to Tuesday index 1 -> 2026-10-23
    intent = await parser.parse("quero cortar o cabelo na sexta às 14:30")

    assert intent.intent is IntentType.BOOK
    assert intent.service_name == "corte"
    assert intent.local_date == date(2026, 10, 23)
    assert intent.local_time == time(14, 30)


async def test_fake_parser_falls_back_to_unknown_for_ambiguous_text() -> None:
    parser = FakeIntentParser()
    intent = await parser.parse("qual é o endereço de vocês?")

    assert intent.intent is IntentType.UNKNOWN
    assert intent.service_name is None
    assert intent.local_date is None
