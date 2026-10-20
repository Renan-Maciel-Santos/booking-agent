"""Deterministic rule-based FakeIntentParser used for local dev, testing, and CI."""

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from booking_agent.domain.booking_intent import BookingIntent, IntentType, TimePreference
from booking_agent.domain.ports import Clock

logger = logging.getLogger(__name__)

_WEEKDAYS = {
    "segunda": 0,
    "terca": 1,
    "terça": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}


class FakeIntentParser:
    """Deterministic rule-based intent parser for testing and development.

    Warns prominently at initialization that LLM calls are mocked.
    Matches canned responses first, then falls back to keyword and relative date rules.
    """

    def __init__(
        self,
        responses: dict[str, BookingIntent] | None = None,
        clock: Clock | None = None,
        business_timezone: ZoneInfo | None = None,
    ) -> None:
        """Initialize with optional canned responses, clock, and business timezone.

        Args:
            responses: Pre-programmed responses keyed by raw user message.
            clock: Clock protocol instance for reference date calculation.
            business_timezone: Salon business timezone.
        """
        logger.warning("Running with FakeIntentParser. LLM calls are mocked.")
        self._responses = dict(responses or {})
        self._clock = clock
        self._tz = business_timezone or ZoneInfo("America/Sao_Paulo")
        self.received_messages: list[str] = []

    def _get_reference_date(self) -> date:
        """Return the reference calendar date from clock or system now."""
        if self._clock is not None:
            return self._clock.now().astimezone(self._tz).date()
        return datetime.now(UTC).astimezone(self._tz).date()

    async def parse(self, user_message: str) -> BookingIntent:
        """Parse user message deterministically.

        Args:
            user_message: Raw text sent by user.

        Returns:
            Extracted BookingIntent.
        """
        self.received_messages.append(user_message)

        # 1. Canned response check
        if user_message in self._responses:
            return self._responses[user_message]

        lower = user_message.lower()

        # 2. Intent extraction
        intent = IntentType.UNKNOWN
        if any(word in lower for word in ["cancelar", "cancela", "desmarcar", "cancel"]):
            intent = IntentType.CANCEL
        elif any(
            word in lower for word in ["remarcar", "remarca", "mudar", "trocar", "reschedule"]
        ):
            intent = IntentType.RESCHEDULE
        elif any(
            phrase in lower
            for phrase in [
                "agendar",
                "agende",
                "marcar",
                "marca",
                "cortar",
                "corte",
                "fazer a barba",
                "degradê",
                "degrade",
                "quero",
                "queria",
                "book",
            ]
        ):
            intent = IntentType.BOOK

        if intent is IntentType.UNKNOWN:
            return BookingIntent(intent=IntentType.UNKNOWN)

        # 3. Service extraction
        service_name: str | None = None
        if "cabelo e barba" in lower or "corte e barba" in lower:
            service_name = "cabelo e barba"
        elif "degradê" in lower or "degrade" in lower or "fade" in lower:
            service_name = "degradê"
        elif (
            "barba na régua" in lower
            or "barba na regua" in lower
            or ("barba" in lower and "cabelo" not in lower and "corte" not in lower)
        ):
            service_name = "barba"
        elif "corte social" in lower:
            service_name = "corte social"
        elif "cabelo" in lower or "corte" in lower:
            service_name = "corte"
        elif "sobrancelha" in lower:
            service_name = "sobrancelha"
        elif "pezinho" in lower:
            service_name = "pezinho"

        # 4. Relative Date extraction
        ref_date = self._get_reference_date()
        local_date: date | None = None
        if "depois de amanhã" in lower or "depois de amanha" in lower:
            local_date = ref_date + timedelta(days=2)
        elif "amanhã" in lower or "amanha" in lower:
            local_date = ref_date + timedelta(days=1)
        elif "hoje" in lower:
            local_date = ref_date
        else:
            for day_name, weekday_idx in _WEEKDAYS.items():
                if day_name in lower:
                    days_ahead = (weekday_idx - ref_date.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    local_date = ref_date + timedelta(days=days_ahead)
                    break

        # 5. Time preference extraction
        time_preference: TimePreference | None = None
        if any(term in lower for term in ["de manhã", "de manha", "pela manhã", "cedo"]):
            time_preference = TimePreference.MORNING
        elif any(
            term in lower for term in ["à tarde", "a tarde", "pela tarde", "depois do almoço"]
        ):
            time_preference = TimePreference.AFTERNOON
        elif any(term in lower for term in ["à noite", "a noite", "pela noite", "fim da tarde"]):
            time_preference = TimePreference.EVENING

        # 6. Specific wall-clock time extraction
        local_time: time | None = None
        # Match e.g. "15:30", "15h30", "15h", "às 14h", "as 14:00"
        time_match = re.search(r"(?:às|as)?\s*(\b\d{1,2})(?:h|:)(\d{2})?\b", lower)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2)) if time_match.group(2) else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                local_time = time(hour, minute)
        elif " às " in lower or " as " in lower:
            simple_hour = re.search(r"(?:às|as)\s*(\b\d{1,2})\b", lower)
            if simple_hour:
                hour = int(simple_hour.group(1))
                if 0 <= hour <= 23:
                    local_time = time(hour, 0)

        # 7. Barber preference extraction
        barber_name: str | None = None
        if "carlos" in lower:
            barber_name = "Carlos"
        elif "renan" in lower:
            barber_name = "Renan"

        # 8. Customer name extraction
        customer_name: str | None = None
        name_match = re.search(r"(?:sou o|me chamo|aqui é o|nome é)\s+([a-zá-ÿ]+)", lower)
        if name_match:
            customer_name = name_match.group(1).capitalize()

        return BookingIntent(
            intent=intent,
            service_name=service_name,
            local_date=local_date,
            local_time=local_time,
            time_preference=time_preference,
            barber_name=barber_name,
            customer_name=customer_name,
        )
