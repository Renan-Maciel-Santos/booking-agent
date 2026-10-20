"""Unit tests for OpenAIIntentParser adapter contract, schemas, and error mapping."""

from datetime import UTC, date, datetime, time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from booking_agent.domain.booking_intent import BookingIntent, IntentType, TimePreference
from booking_agent.domain.errors import (
    LLMConfigurationError,
    LLMInvalidOutputError,
    LLMUnavailableError,
)
from booking_agent.integrations.llm.openai_intent_parser import (
    OpenAIIntentParser,
    _LLMBookingIntentSchema,
)
from tests.fakes.fake_clock import FakeClock

TEST_TZ = ZoneInfo("America/Sao_Paulo")
FIXED_NOW_UTC = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def _build_mock_response(
    parsed: _LLMBookingIntentSchema | None,
    refusal: str | None = None,
    finish_reason: str = "stop",
) -> MagicMock:
    """Helper creating a mock ChatCompletion response structure."""
    choice = MagicMock()
    choice.message.parsed = parsed
    choice.message.refusal = refusal
    choice.finish_reason = finish_reason

    response = MagicMock()
    response.choices = [choice]
    return response


async def test_openai_parser_contract_with_recorded_response() -> None:
    # Arrange: recorded LLM response conforming to _LLMBookingIntentSchema
    recorded_payload = _LLMBookingIntentSchema(
        intent=IntentType.BOOK,
        service_name="corte",
        local_date="2026-10-21",
        local_time="15:00:00",
        time_preference=TimePreference.AFTERNOON,
        barber_name="Carlos",
        customer_name="Marcos",
    )
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        return_value=_build_mock_response(recorded_payload)
    )

    clock = FakeClock(FIXED_NOW_UTC)
    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=clock,
        business_timezone=TEST_TZ,
        timeout=5.0,
    )

    # Act
    intent = await parser.parse(
        "opa, queria cortar o cabelo amanha as 15h com o carlos, sou o marcos"
    )

    # Assert domain BookingIntent conversion
    assert intent == BookingIntent(
        intent=IntentType.BOOK,
        service_name="corte",
        local_date=date(2026, 10, 21),
        local_time=time(15, 0),
        time_preference=TimePreference.AFTERNOON,
        barber_name="Carlos",
        customer_name="Marcos",
    )

    # Assert client call payload structure and prompt fences
    call_kwargs = mock_client.beta.chat.completions.parse.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["response_format"] == _LLMBookingIntentSchema
    messages = call_kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "Current Date: 2026-10-20" in messages[0]["content"]
    assert "Business Timezone: America/Sao_Paulo" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    expected_user_msg = (
        "<user_message>\n"
        "opa, queria cortar o cabelo amanha as 15h com o carlos, sou o marcos\n"
        "</user_message>"
    )
    assert expected_user_msg in messages[1]["content"]


@pytest.mark.parametrize(
    "sdk_error",
    [
        APIConnectionError(request=MagicMock()),
        RateLimitError(message="rate limited", response=MagicMock(), body=None),
        APITimeoutError(request=MagicMock()),
        InternalServerError(message="internal server error", response=MagicMock(), body=None),
    ],
)
async def test_openai_parser_maps_transient_errors_to_llm_unavailable(
    sdk_error: Exception,
) -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(side_effect=sdk_error)

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMUnavailableError) as exc_info:
        await parser.parse("quero agendar um corte")

    assert exc_info.value.__cause__ is sdk_error


@pytest.mark.parametrize(
    "config_error",
    [
        AuthenticationError(message="invalid api key (401)", response=MagicMock(), body=None),
        NotFoundError(message="model not found (404)", response=MagicMock(), body=None),
        PermissionDeniedError(message="permission denied (403)", response=MagicMock(), body=None),
        BadRequestError(message="bad request (400)", response=MagicMock(), body=None),
    ],
)
async def test_openai_parser_maps_configuration_errors_to_llm_configuration_error(
    config_error: Exception,
) -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(side_effect=config_error)

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMConfigurationError) as exc_info:
        await parser.parse("quero agendar um corte")

    assert exc_info.value.__cause__ is config_error


async def test_openai_parser_maps_refusal_to_invalid_output_error() -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        return_value=_build_mock_response(parsed=None, refusal="I cannot fulfill this request")
    )

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMInvalidOutputError, match="Model refused prompt"):
        await parser.parse("hacked instructions")


async def test_openai_parser_maps_length_truncation_to_invalid_output_error() -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        side_effect=LengthFinishReasonError(completion=MagicMock())
    )

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMInvalidOutputError, match="truncated or filtered"):
        await parser.parse("very long message")


async def test_openai_parser_maps_content_filter_to_invalid_output_error() -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        side_effect=ContentFilterFinishReasonError()
    )

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMInvalidOutputError, match="truncated or filtered"):
        await parser.parse("unsafe message")


async def test_openai_parser_maps_none_parsed_output_to_invalid_output_error() -> None:
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        return_value=_build_mock_response(parsed=None, refusal=None)
    )

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMInvalidOutputError, match="no structured output"):
        await parser.parse("any message")


async def test_openai_parser_maps_malformed_date_to_invalid_output_error() -> None:
    # Model returned text that fails ISO date parsing
    invalid_date_payload = _LLMBookingIntentSchema(
        intent=IntentType.BOOK,
        local_date="not-a-date",
        local_time="15:00:00",
    )
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(
        return_value=_build_mock_response(invalid_date_payload)
    )

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(LLMInvalidOutputError, match="Invalid date/time format"):
        await parser.parse("quero cortar amanhã")


async def test_openai_parser_maps_api_call_validation_error_to_invalid_output_error() -> None:
    validation_err = ValidationError.from_exception_data("StructuredOutput", [])
    mock_client = MagicMock(spec=AsyncOpenAI)
    mock_client.beta = MagicMock()
    mock_client.beta.chat = MagicMock()
    mock_client.beta.chat.completions = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(side_effect=validation_err)

    parser = OpenAIIntentParser(
        client=mock_client,
        model="gpt-4o-mini",
        clock=FakeClock(FIXED_NOW_UTC),
        business_timezone=TEST_TZ,
    )

    with pytest.raises(
        LLMInvalidOutputError, match="validation failed during API parse"
    ) as exc_info:
        await parser.parse("any message")

    assert exc_info.value.__cause__ is validation_err


def test_adapter_schema_rejects_extra_fields() -> None:
    # Strict mode test: extra invented attributes are strictly forbidden
    with pytest.raises(ValidationError):
        _LLMBookingIntentSchema.model_validate(
            {"intent": "book", "extra_injected_field": "malicious"}
        )
