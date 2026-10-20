"""OpenAI structured outputs implementation of IntentParser."""

from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

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
    UnprocessableEntityError,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from booking_agent.domain.booking_intent import BookingIntent, IntentType, TimePreference
from booking_agent.domain.errors import (
    LLMConfigurationError,
    LLMInvalidOutputError,
    LLMUnavailableError,
)
from booking_agent.domain.ports import Clock

PROMPT_VERSION = "2026-10-20.v1"
DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "v1.txt"


def load_prompt_template(path: Path | None = None) -> str:
    """Load prompt template from file.

    Args:
        path: Optional file path. Defaults to v1.txt.

    Returns:
        Prompt template string.
    """
    target = path or DEFAULT_PROMPT_PATH
    return target.read_text(encoding="utf-8")


class _LLMBookingIntentSchema(BaseModel):
    """Adapter-level schema conforming to OpenAI Structured Outputs strict requirements."""

    model_config = ConfigDict(extra="forbid")

    intent: IntentType
    service_name: str | None = None
    local_date: str | None = None
    local_time: str | None = None
    time_preference: TimePreference | None = None
    barber_name: str | None = None
    customer_name: str | None = None


class OpenAIIntentParser:
    """Turns raw user messages into validated BookingIntent instances via OpenAI."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        clock: Clock,
        business_timezone: ZoneInfo,
        prompt_template: str | None = None,
        temperature: float = 0.0,
        timeout: float = 10.0,
    ) -> None:
        """Initialize the parser with injected client, settings, and clock.

        Args:
            client: Configured AsyncOpenAI client.
            model: OpenAI model identifier (e.g. gpt-4o-mini).
            clock: Clock implementation for relative reference time.
            business_timezone: Salon business timezone for date/time formatting.
            prompt_template: Optional prompt template string.
            temperature: Sampling temperature for extraction.
            timeout: Per-request timeout in seconds.
        """
        self._client = client
        self._model = model
        self._clock = clock
        self._tz = business_timezone
        self._prompt_template = (
            prompt_template if prompt_template is not None else load_prompt_template()
        )
        self._temperature = temperature
        self._timeout = timeout

    async def parse(self, user_message: str) -> BookingIntent:
        """Extract intent and entities from a raw user message.

        Args:
            user_message: Untrusted customer input text.

        Returns:
            Validated domain BookingIntent.

        Raises:
            LLMConfigurationError: If OpenAI rejects credentials (401), permissions (403),
                model name (404), or request parameters (400/422).
            LLMUnavailableError: If OpenAI is unreachable, timed out, or rate-limited.
            LLMInvalidOutputError: If model refuses, response is truncated/content-filtered,
                or schema/domain validation fails.
        """
        now = self._clock.now().astimezone(self._tz)
        system_content = self._prompt_template.format(
            reference_date=now.strftime("%Y-%m-%d"),
            reference_time=now.strftime("%H:%M:%S"),
            reference_weekday=now.strftime("%A"),
            business_timezone=str(self._tz),
        )
        user_content = f"<user_message>\n{user_message}\n</user_message>"

        try:
            response = await self._client.beta.chat.completions.parse(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                response_format=_LLMBookingIntentSchema,
                temperature=self._temperature,
                timeout=self._timeout,
            )
        except (
            APIConnectionError,
            RateLimitError,
            APITimeoutError,
            InternalServerError,
        ) as exc:
            raise LLMUnavailableError(f"OpenAI service unavailable: {exc}") from exc
        except (LengthFinishReasonError, ContentFilterFinishReasonError) as exc:
            raise LLMInvalidOutputError(f"LLM output truncated or filtered: {exc}") from exc
        except ValidationError as exc:
            raise LLMInvalidOutputError(
                f"Structured outputs schema validation failed during API parse: {exc}"
            ) from exc
        except (
            AuthenticationError,
            PermissionDeniedError,
            NotFoundError,
            BadRequestError,
            UnprocessableEntityError,
        ) as exc:
            raise LLMConfigurationError(f"OpenAI configuration error: {exc}") from exc

        choice = response.choices[0]
        if choice.message.refusal:
            raise LLMInvalidOutputError(f"Model refused prompt: {choice.message.refusal}")

        parsed = choice.message.parsed
        if parsed is None:
            raise LLMInvalidOutputError("Model produced no structured output")

        try:
            parsed_date = date.fromisoformat(parsed.local_date) if parsed.local_date else None
            parsed_time = time.fromisoformat(parsed.local_time) if parsed.local_time else None
        except ValueError as exc:
            raise LLMInvalidOutputError(f"Invalid date/time format in model output: {exc}") from exc

        try:
            return BookingIntent(
                intent=parsed.intent,
                service_name=parsed.service_name,
                local_date=parsed_date,
                local_time=parsed_time,
                time_preference=parsed.time_preference,
                barber_name=parsed.barber_name,
                customer_name=parsed.customer_name,
            )
        except ValidationError as exc:
            raise LLMInvalidOutputError(f"Failed to construct BookingIntent: {exc}") from exc
