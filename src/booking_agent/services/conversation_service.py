"""ConversationService orchestrating multi-turn chat interactions across 6 phases."""

import logging
import re
import time as time_module
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, assert_never
from zoneinfo import ZoneInfo

from booking_agent.config import Settings
from booking_agent.database.models import Appointment
from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.conversation_state import (
    ANY_BARBER,
    ConversationSnapshot,
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConfirmationCandidate,
    ConversationFlow,
    ConversationStage,
    MessageStatus,
)
from booking_agent.domain.errors import (
    InvalidConversationStateError,
    LLMInvalidOutputError,
    MissingConfirmationDataError,
    SlotUnavailableError,
)
from booking_agent.domain.ports import (
    Clock,
    IntentParser,
    UnitOfWork,
    UnitOfWorkFactory,
)
from booking_agent.services.booking_service import BookingService, localize_datetime
from booking_agent.services.entity_resolver import EntityResolver, normalize_text
from booking_agent.services.templates import (
    TemplateKey,
    format_currency_brl,
    format_short_date_pt_br,
    format_time_pt_br,
    render_flow_cancelled,
    render_template,
)

logger = logging.getLogger(__name__)


def mask_phone(phone: str) -> str:
    """Mask phone number preserving country code and last 4 digits for privacy.

    Example: '+5511999998888' -> '+5511*****8888'.
    """
    if len(phone) <= 8:
        return "*" * len(phone)
    prefix = phone[:5]
    suffix = phone[-4:]
    return f"{prefix}*****{suffix}"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome and observability telemetry for a processed conversation turn."""

    response_text: str | None
    status_code: int = 200
    outcome: str = "ok"
    fast_path: bool = False
    stage_from: str | None = None
    stage_to: str | None = None
    phase1_lease_ms: float = 0.0
    phase2_llm_ms: float = 0.0
    phase3_state_ms: float = 0.0
    phase4_action_ms: float = 0.0
    total_duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ConfirmationDetails:
    """Typed details for rendering deterministic confirmation messages."""

    service: str
    date: str
    time: str
    name: str
    price: str
    old_date_time: str | None = None
    barber: str = "qualquer barbeiro disponível"

    @classmethod
    def from_slots(
        cls,
        slots: dict[str, Any],
        business_tz: ZoneInfo,
    ) -> "ConfirmationDetails":
        """Build ConfirmationDetails from conversation slots, validating mandatory fields."""
        missing: list[str] = []

        service_val = slots.get("service_name")
        if not service_val:
            missing.append("service")

        raw_date = slots.get("local_date")
        formatted_date: str | None = None
        if not raw_date:
            missing.append("date")
        elif isinstance(raw_date, date):
            formatted_date = format_short_date_pt_br(raw_date)
        elif isinstance(raw_date, str):
            try:
                formatted_date = format_short_date_pt_br(date.fromisoformat(raw_date))
            except ValueError:
                missing.append("date")
        else:
            missing.append("date")

        raw_time = slots.get("local_time")
        formatted_time: str | None = None
        if not raw_time:
            missing.append("time")
        elif isinstance(raw_time, time):
            formatted_time = format_time_pt_br(raw_time)
        elif isinstance(raw_time, str):
            try:
                formatted_time = format_time_pt_br(time.fromisoformat(raw_time))
            except ValueError:
                missing.append("time")
        else:
            missing.append("time")

        name_val = slots.get("customer_name")
        if not name_val:
            missing.append("name")

        raw_price = slots.get("service_price_cents")
        formatted_price: str | None = None
        if raw_price is not None and isinstance(raw_price, int):
            formatted_price = format_currency_brl(raw_price)
        else:
            missing.append("price")

        if (
            missing
            or service_val is None
            or formatted_date is None
            or formatted_time is None
            or name_val is None
            or formatted_price is None
        ):
            raise MissingConfirmationDataError(tuple(missing))

        barber_val = slots.get("barber_name") or "qualquer barbeiro disponível"
        old_dt_val = slots.get("old_date_time")

        return cls(
            service=str(service_val),
            date=formatted_date,
            time=formatted_time,
            name=str(name_val),
            price=formatted_price,
            old_date_time=str(old_dt_val) if old_dt_val is not None else None,
            barber=str(barber_val),
        )


_AFFIRMATIVE_TOKENS: set[str] = {
    "sim",
    "s",
    "ok",
    "confirmo",
    "confirmar",
    "confirmado",
    "confirmada",
    "pode confirmar",
    "fechado",
    "beleza",
    "isso",
    "com certeza",
}

_NEGATIVE_TOKENS: set[str] = {
    "nao",
    "não",
    "n",
    "deixa",
    "esquece",
}

_CANCEL_FLOW_TOKENS: set[str] = {
    "cancela",
    "cancelar",
    "pode cancelar",
    "sim pode cancelar",
}

_CUSTOMER_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ '-]{2,60}$")

_KNOWN_COMMANDS: frozenset[str] = frozenset(
    {
        "ajuda",
        "help",
        "menu",
        "cancelar",
        "cancela",
        "cancelamento",
        "remarcar",
        "remarcacao",
        "remarcação",
        "agendar",
        "agendamento",
        "marcar",
        "servicos",
        "serviços",
        "barbeiros",
        "horarios",
        "horários",
        "valores",
        "valor",
        "preco",
        "precos",
        "preços",
        "preço",
    }
)

_COMMAND_OR_QUESTION_WORDS: frozenset[str] = frozenset(
    {
        "qual",
        "quais",
        "quanto",
        "quantos",
        "como",
        "quando",
        "onde",
        "porque",
        "por que",
        "valor",
        "valores",
        "preco",
        "preço",
        "precos",
        "preços",
        "horario",
        "horário",
        "horarios",
        "horários",
        "corte",
        "barba",
        "cabelo",
        "barbeiro",
        "barbeiros",
        "ajuda",
        "cancelar",
        "cancela",
        "remarcar",
        "agendar",
        "marcar",
    }
)


def is_valid_customer_name(name: str) -> bool:
    """Validate customer name per conversation rules.

    Accepts 2 to 60 characters composed exclusively of letters (with accents),
    spaces, hyphens, and apostrophes. Must contain at least one letter.
    Rejects affirmative/negative tokens, known commands, and question/command phrases.
    """
    stripped = name.strip()
    if not (2 <= len(stripped) <= 60):
        return False
    if not _CUSTOMER_NAME_PATTERN.match(stripped):
        return False
    if not any(char.isalpha() for char in stripped):
        return False
    normalized = normalize_text(stripped)
    if (
        normalized in _AFFIRMATIVE_TOKENS
        or normalized in _NEGATIVE_TOKENS
        or normalized in _CANCEL_FLOW_TOKENS
        or normalized in _KNOWN_COMMANDS
    ):
        return False
    words = set(normalized.split())
    return not bool(words & _COMMAND_OR_QUESTION_WORDS)


def _extract_selection_index(text: str, max_count: int) -> int | None:
    """Extract an ordinal or numeric index (0-based) from customer reply."""
    cleaned = normalize_text(text)
    match = re.search(r"\b(\d+)\b", cleaned)
    if match:
        idx = int(match.group(1)) - 1
        if 0 <= idx < max_count:
            return idx
    word_map = {
        "primeiro": 0,
        "primeira": 0,
        "1o": 0,
        "1a": 0,
        "um": 0,
        "segundo": 1,
        "segunda": 1,
        "2o": 1,
        "2a": 1,
        "dois": 1,
        "terceiro": 2,
        "terceira": 2,
        "3o": 2,
        "3a": 2,
        "tres": 2,
    }
    for word, idx in word_map.items():
        if word in cleaned and idx < max_count:
            return idx
    return None


def _extract_alternative_index(text: str, alternatives: list[str]) -> int | None:
    """Extract which alternative slot the user selected by ordinal or time match."""
    cleaned = normalize_text(text)
    idx = _extract_selection_index(text, len(alternatives))
    if idx is not None:
        return idx
    for i, alt_iso in enumerate(alternatives):
        dt = datetime.fromisoformat(alt_iso)
        time_str_1 = dt.strftime("%H:%M")
        time_str_2 = f"{dt.hour}h"
        time_str_3 = f"{dt.hour}:{dt.minute:02d}"
        if time_str_1 in cleaned or time_str_2 in cleaned or time_str_3 in cleaned:
            return i
    return None


def classify_fast_path_candidate(text: str) -> ConfirmationCandidate | None:
    """Classify user text into a confirmation candidate category, or None if non-exact."""
    normalized_msg = normalize_text(text)
    if normalized_msg in _AFFIRMATIVE_TOKENS:
        return ConfirmationCandidate.AFFIRMATIVE
    if normalized_msg in _NEGATIVE_TOKENS:
        return ConfirmationCandidate.NEGATIVE
    if normalized_msg in _CANCEL_FLOW_TOKENS:
        return ConfirmationCandidate.CANCEL_ACTION
    return None


def resolve_confirmation(candidate: ConfirmationCandidate, flow: ConversationFlow) -> bool:
    """Disambiguate confirmation candidate tokens based on the pending flow.

    Args:
        candidate: Fast-path confirmation candidate category.
        flow: Currently active conversation flow.

    Returns:
        True if the action should proceed to execution, False if the action is cancelled.
    """
    match candidate:
        case ConfirmationCandidate.AFFIRMATIVE:
            return True
        case ConfirmationCandidate.NEGATIVE:
            return False
        case ConfirmationCandidate.CANCEL_ACTION:
            return flow == ConversationFlow.CANCEL
        case _ as unreachable:
            assert_never(unreachable)


def _barber_filter(slots: dict[str, Any]) -> uuid.UUID | None:
    """Extract optional barber UUID filter from slots, returning None for 'any' or unset."""
    raw_id = slots.get("barber_id")
    if not raw_id or raw_id == ANY_BARBER:
        return None
    return uuid.UUID(str(raw_id))


class ConversationService:
    """Multi-turn conversation orchestrator managing dialogue state and booking actions."""

    def __init__(
        self,
        intent_parser: IntentParser,
        booking_service: BookingService,
        entity_resolver: EntityResolver,
        clock: Clock,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
    ) -> None:
        """Initialize dependencies."""
        self._intent_parser = intent_parser
        self._booking_service = booking_service
        self._entity_resolver = entity_resolver
        self._clock = clock
        self._uow_factory = uow_factory
        self._settings = settings
        self._business_tz = ZoneInfo(settings.business_timezone)

    async def _save_state(
        self,
        uow: UnitOfWork,
        phone: str,
        stage: str,
        slots: dict[str, Any],
    ) -> ConversationSnapshot:
        """Save conversation state converting dict to typed domain model and verifying schema.

        A validation failure on save indicates an internal programmer error and propagates
        directly as a ValidationError.

        Args:
            uow: Active unit of work.
            phone: Customer phone number.
            stage: Target conversation stage string.
            slots: Target conversation slots dictionary.

        Returns:
            Saved conversation snapshot.
        """
        typed_stage = ConversationStage(stage)
        typed_slots = slots_from_json(slots)
        return await uow.conversation_states.upsert(phone, typed_stage, typed_slots)

    def _enter_confirmation(
        self,
        flow: ConversationFlow,
        slots: dict[str, Any],
        details: ConfirmationDetails,
    ) -> tuple[str, dict[str, Any], str]:
        """Single entry point for transitioning the conversation into AWAITING_CONFIRMATION.

        Generates a new action_id, strictly records slots["flow"], and chooses the
        exact confirmation template corresponding to the flow using exhaustive matching.

        Args:
            flow: Active ConversationFlow member.
            slots: Slot dictionary to persist with the flow and action_id.
            details: Mandatory typed details for template rendering.

        Returns:
            Tuple of (stage, slots, confirmation_prompt).

        Raises:
            MissingConfirmationDataError: If old_date_time is missing for RESCHEDULE flow.
        """
        if flow == ConversationFlow.RESCHEDULE and not details.old_date_time:
            raise MissingConfirmationDataError(("old_date_time",))

        slots["action_id"] = str(uuid.uuid4())
        slots["flow"] = flow.value
        slots.pop("awaiting", None)

        match flow:
            case ConversationFlow.BOOK:
                prompt = render_template(
                    TemplateKey.CONFIRM_BOOKING,
                    service=details.service,
                    date=details.date,
                    time=details.time,
                    barber=details.barber,
                    name=details.name,
                    price=details.price,
                )
            case ConversationFlow.RESCHEDULE:
                if not details.old_date_time:
                    raise MissingConfirmationDataError(("old_date_time",))
                prompt = render_template(
                    TemplateKey.CONFIRM_RESCHEDULE,
                    service=details.service,
                    old_date_time=details.old_date_time,
                    new_date=details.date,
                    new_time=details.time,
                )
            case ConversationFlow.CANCEL:
                prompt = render_template(
                    TemplateKey.CONFIRM_CANCEL,
                    service=details.service,
                    date=details.date,
                    time=details.time,
                    barber=details.barber,
                )
            case _ as unreachable:
                assert_never(unreachable)

        return ConversationStage.AWAITING_CONFIRMATION.value, slots, prompt

    def _handle_missing_confirmation_data(
        self,
        phone: str,
        slots: dict[str, Any],
        error: MissingConfirmationDataError,
    ) -> tuple[str, dict[str, Any], str]:
        """Handle missing confirmation details: log event without PII and fail safe to IDLE."""
        logger.error(
            "missing_confirmation_data",
            extra={
                "event": "missing_confirmation_data",
                "missing_fields": error.missing_fields,
                "phone_last4": mask_phone(phone),
            },
        )
        slots.clear()
        return (
            ConversationStage.IDLE.value,
            slots,
            render_template(TemplateKey.CONFIRMATION_DATA_UNAVAILABLE),
        )

    async def _handle_invalid_conversation_state(
        self,
        uow: UnitOfWork,
        phone: str,
        message_id: str,
        error: InvalidConversationStateError,
        t3_start: float,
        t0: float,
        phase1_ms: float,
        phase2_llm_ms: float,
        initial_stage: str,
    ) -> TurnResult:
        """Handle invalid persisted state: log structured event, reset to IDLE and fail safe."""
        logger.error(
            "invalid_conversation_state",
            extra={
                "event": "invalid_conversation_state",
                "invalid_fields": error.invalid_fields,
                "phone_last4": mask_phone(phone),
            },
        )
        stage = ConversationStage.IDLE.value
        slots: dict[str, Any] = {}
        await self._save_state(uow, phone, stage, slots)
        response = render_template(TemplateKey.CONVERSATION_RESET)
        await uow.processed_messages.mark_done(message_id, response_text=response)
        phase3_ms = (time_module.perf_counter() - t3_start) * 1000
        total_ms = (time_module.perf_counter() - t0) * 1000
        return TurnResult(
            response_text=response,
            status_code=200,
            outcome="conversation_reset",
            stage_from=initial_stage,
            stage_to=stage,
            phase1_lease_ms=phase1_ms,
            phase2_llm_ms=phase2_llm_ms,
            phase3_state_ms=phase3_ms,
            total_duration_ms=total_ms,
        )

    def _extract_pending_flow(
        self,
        stage: str,
        slots: dict[str, Any],
        phone: str,
    ) -> ConversationFlow | None:
        """Extract and validate the pending flow from state slots.

        If missing or unrecognized, logs an error event without personal data
        and returns None to trigger a fail-safe reset to IDLE.

        Args:
            stage: Current conversation stage.
            slots: State slots dictionary.
            phone: Customer phone number (masked in logs).

        Returns:
            Validated ConversationFlow member, or None if missing or invalid.
        """
        raw_flow = slots.get("flow")
        masked_phone = mask_phone(phone)
        if not raw_flow:
            logger.error(
                "Missing pending flow in state slots for conversation",
                extra={
                    "event": "missing_pending_flow",
                    "phone_last4": masked_phone,
                    "stage": stage,
                },
            )
            return None

        try:
            return ConversationFlow(raw_flow)
        except ValueError:
            logger.error(
                "Invalid pending flow '%s' in state slots for conversation",
                raw_flow,
                extra={
                    "event": "missing_pending_flow",
                    "phone_last4": masked_phone,
                    "stage": stage,
                },
            )
            return None

    # Backward compatibility alias
    _get_required_flow = _extract_pending_flow

    async def _load_reschedule_origin(
        self,
        uow: UnitOfWork,
        phone: str,
        slots: dict[str, Any],
    ) -> Appointment | None:
        """Validate and load the confirmed origin appointment for rescheduling.

        Ensures appointment_id is present and a valid UUID, the appointment exists,
        is confirmed, and belongs to the customer of this phone number.
        Populates service_id, barber_id, and old_date_time in slots.

        Args:
            uow: Active unit of work.
            phone: Customer phone number.
            slots: State slots dictionary.

        Returns:
            The confirmed Appointment if valid, None on any failure.
        """
        masked_phone = mask_phone(phone)

        def _reject(reason: str) -> None:
            logger.error(
                "invalid_reschedule_origin",
                extra={
                    "event": "invalid_reschedule_origin",
                    "reason": reason,
                    "phone_last4": masked_phone,
                },
            )

        raw_id = slots.get("appointment_id")
        if not raw_id:
            _reject("missing_id")
            return None

        try:
            aid = uuid.UUID(str(raw_id))
        except (ValueError, AttributeError):
            _reject("invalid_id")
            return None

        appt = await uow.appointments.get_by_id(aid)
        if appt is None:
            _reject("not_found")
            return None

        if appt.status != AppointmentStatus.CONFIRMED.value:
            _reject("not_confirmed")
            return None

        customer = await uow.customers.get_by_phone(phone)
        if customer is None or appt.customer_id != customer.id:
            _reject("not_owned")
            return None

        if "service_id" not in slots:
            slots["service_id"] = str(appt.service_id)
        if "barber_id" not in slots:
            slots["barber_id"] = str(appt.barber_id)
        if "service_name" not in slots or "service_price_cents" not in slots:
            svc = await uow.services.get_by_id(appt.service_id)
            if svc:
                slots.setdefault("service_name", svc.name)
                slots.setdefault("service_price_cents", svc.price_cents)
        if "barber_name" not in slots and slots.get("barber_id") != ANY_BARBER:
            brb = await uow.barbers.get_by_id(appt.barber_id)
            if brb:
                slots["barber_name"] = brb.name
        if "customer_name" not in slots and customer:
            slots["customer_name"] = customer.name
        if "old_date_time" not in slots:
            start_local = appt.starts_at.astimezone(self._business_tz)
            slots["old_date_time"] = (
                f"{format_short_date_pt_br(start_local.date())} às "
                f"{format_time_pt_br(start_local.time())}"
            )

        return appt

    async def handle_message(self, message_id: str, phone: str, text: str) -> str:
        """Process an incoming chat message and return response text for user."""
        result = await self.process_turn(message_id=message_id, phone=phone, text=text)
        return result.response_text or ""

    async def process_turn(self, message_id: str, phone: str, text: str) -> TurnResult:
        """Process an incoming chat message across the 6 phases, returning rich telemetry."""
        t0 = time_module.perf_counter()

        # -------------------------------------------------------------
        # Phase 1: Atomic Idempotency Lease (Short Tx)
        # -------------------------------------------------------------
        async with self._uow_factory() as uow:
            acquisition = await uow.processed_messages.acquire(
                message_id=message_id,
                timeout_seconds=self._settings.message_lease_timeout_seconds,
            )
        t1 = time_module.perf_counter()
        phase1_ms = (t1 - t0) * 1000

        if not acquisition.acquired:
            total_ms = (time_module.perf_counter() - t0) * 1000
            if acquisition.status == MessageStatus.DONE:
                return TurnResult(
                    response_text=acquisition.response_text
                    or render_template(TemplateKey.GREETING_HELP),
                    status_code=200,
                    outcome="idempotent_replay",
                    phase1_lease_ms=phase1_ms,
                    total_duration_ms=total_ms,
                )
            # Replayed message is still actively processing within lease:
            # HTTP 202 Accepted, response_text=None
            return TurnResult(
                response_text=None,
                status_code=202,
                outcome="duplicate_in_progress",
                phase1_lease_ms=phase1_ms,
                total_duration_ms=total_ms,
            )

        try:
            return await self._process_turn(message_id, phone, text, t0, phase1_ms)
        except Exception:
            # Catch-all is strictly for state cleanup (releasing the message lease by marking
            # it as failed in its own short transaction) before re-raising the original exception;
            # the error is never suppressed.
            async with self._uow_factory() as uow:
                await uow.processed_messages.mark_failed(message_id)
            raise

    async def _process_turn(
        self,
        message_id: str,
        phone: str,
        text: str,
        t0: float = 0.0,
        phase1_ms: float = 0.0,
    ) -> TurnResult:
        """Internal turn processor following Phases 2 through 6."""
        # -------------------------------------------------------------
        # Phase 2: Candidate Fast-Path Pre-Evaluation & LLM Extraction
        # (Zero DB Connections or locks Held)
        # -------------------------------------------------------------
        fast_path_candidate = classify_fast_path_candidate(text)

        intent: BookingIntent | None = None
        phase2_llm_ms = 0.0
        if fast_path_candidate is None:
            t_llm0 = time_module.perf_counter()
            try:
                intent = await self._intent_parser.parse(user_message=text)
                phase2_llm_ms = (time_module.perf_counter() - t_llm0) * 1000
            except LLMInvalidOutputError:
                phase2_llm_ms = (time_module.perf_counter() - t_llm0) * 1000
                clarify_resp = render_template(TemplateKey.CLARIFICATION_PROMPT)
                async with self._uow_factory() as uow:
                    await uow.processed_messages.mark_done(message_id, response_text=clarify_resp)
                total_ms = (time_module.perf_counter() - t0) * 1000
                return TurnResult(
                    response_text=clarify_resp,
                    status_code=200,
                    outcome="llm_clarification",
                    fast_path=False,
                    phase1_lease_ms=phase1_ms,
                    phase2_llm_ms=phase2_llm_ms,
                    total_duration_ms=total_ms,
                )

        # -------------------------------------------------------------
        # Phase 3: State Lock, TTL, Fast-Path Revalidation & Slot Merge (Short Tx)
        # -------------------------------------------------------------
        t3_start = time_module.perf_counter()
        stage: str
        initial_stage: str = ConversationStage.IDLE.value
        slots: dict[str, Any]
        response: str | None = None
        needs_llm_outside_lock = False

        async with self._uow_factory() as uow:
            try:
                state = await uow.conversation_states.get(phone, for_update=True)
            except InvalidConversationStateError as exc:
                return await self._handle_invalid_conversation_state(
                    uow=uow,
                    phone=phone,
                    message_id=message_id,
                    error=exc,
                    t3_start=t3_start,
                    t0=t0,
                    phase1_ms=phase1_ms,
                    phase2_llm_ms=phase2_llm_ms,
                    initial_stage=initial_stage,
                )

            if state is None:
                stage = ConversationStage.IDLE.value
                initial_stage = stage
                slots = {}
                is_expired = False
            else:
                stage = state.stage.value
                initial_stage = stage
                slots = slots_to_json(state.slots)
                elapsed = self._clock.now() - state.updated_at
                is_expired = elapsed > timedelta(minutes=self._settings.conversation_ttl_minutes)

            # TTL expiration handling
            if is_expired:
                was_in_progress = stage != ConversationStage.IDLE.value or bool(slots)
                stage = ConversationStage.IDLE.value
                slots = {}
                await self._save_state(uow, phone, stage, slots)
                if was_in_progress:
                    response = render_template(TemplateKey.SESSION_EXPIRED)
                    await uow.processed_messages.mark_done(message_id, response_text=response)
                    phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                    total_ms = (time_module.perf_counter() - t0) * 1000
                    return TurnResult(
                        response_text=response,
                        status_code=200,
                        outcome="session_expired",
                        stage_from=initial_stage,
                        stage_to=stage,
                        phase1_lease_ms=phase1_ms,
                        phase2_llm_ms=phase2_llm_ms,
                        phase3_state_ms=phase3_ms,
                        total_duration_ms=total_ms,
                    )

            # If stage is EXECUTING: either concurrent execution, or stuck execution
            if stage == ConversationStage.EXECUTING.value:
                lease_timeout = timedelta(seconds=self._settings.message_lease_timeout_seconds)
                is_executing_stuck = elapsed > lease_timeout
                if slots.get("executing_message_id") == message_id or is_executing_stuck:
                    # Retry of the crashed message itself OR next message after lease timeout
                    # (stuck EXECUTING): resume execution with the same action_id
                    slots["executing_message_id"] = message_id
                    await self._save_state(uow, phone, stage, slots)
                else:
                    response = render_template(TemplateKey.REQUEST_PROCESSING)
                    await uow.processed_messages.mark_done(message_id, response_text=response)
                    phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                    total_ms = (time_module.perf_counter() - t0) * 1000
                    return TurnResult(
                        response_text=response,
                        status_code=200,
                        outcome="request_processing",
                        stage_from=initial_stage,
                        stage_to=stage,
                        phase1_lease_ms=phase1_ms,
                        phase2_llm_ms=phase2_llm_ms,
                        phase3_state_ms=phase3_ms,
                        total_duration_ms=total_ms,
                    )

            # Customer recognition: lookup registered customer name
            customer = await uow.customers.get_by_phone(phone)
            if customer is not None:
                slots["customer_name"] = customer.name

            # Fast-path revalidation under lock
            if fast_path_candidate is not None:
                if stage == ConversationStage.AWAITING_CONFIRMATION.value and "action_id" in slots:
                    validated_flow = self._get_required_flow(stage, slots, phone)
                    if validated_flow is None:
                        stage = ConversationStage.IDLE.value
                        slots = {}
                        await self._save_state(uow, phone, stage, slots)
                        response = render_template(TemplateKey.GREETING_HELP)
                        await uow.processed_messages.mark_done(message_id, response_text=response)
                        phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                        total_ms = (time_module.perf_counter() - t0) * 1000
                        return TurnResult(
                            response_text=response,
                            status_code=200,
                            outcome="missing_pending_flow",
                            fast_path=True,
                            stage_from=initial_stage,
                            stage_to=stage,
                            phase1_lease_ms=phase1_ms,
                            phase2_llm_ms=phase2_llm_ms,
                            phase3_state_ms=phase3_ms,
                            total_duration_ms=total_ms,
                        )

                    is_affirmative = resolve_confirmation(fast_path_candidate, validated_flow)

                    if is_affirmative:
                        try:
                            details = ConfirmationDetails.from_slots(slots, self._business_tz)
                            if (
                                validated_flow == ConversationFlow.RESCHEDULE
                                and not details.old_date_time
                            ):
                                raise MissingConfirmationDataError(("old_date_time",))
                        except MissingConfirmationDataError as exc:
                            stage, slots, response = self._handle_missing_confirmation_data(
                                phone, slots, exc
                            )
                            await self._save_state(uow, phone, stage, slots)
                            await uow.processed_messages.mark_done(
                                message_id, response_text=response
                            )
                            phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                            total_ms = (time_module.perf_counter() - t0) * 1000
                            return TurnResult(
                                response_text=response,
                                status_code=200,
                                outcome="missing_confirmation_data",
                                fast_path=True,
                                stage_from=initial_stage,
                                stage_to=stage,
                                phase1_lease_ms=phase1_ms,
                                phase2_llm_ms=phase2_llm_ms,
                                phase3_state_ms=phase3_ms,
                                total_duration_ms=total_ms,
                            )
                        stage = ConversationStage.EXECUTING.value
                        slots["executing_message_id"] = message_id
                        await self._save_state(uow, phone, stage, slots)
                        # Lock released upon exiting block; advances to Phase 4
                    else:
                        response = render_flow_cancelled(validated_flow)
                        stage = ConversationStage.IDLE.value
                        slots = {}
                        await self._save_state(uow, phone, stage, slots)
                        await uow.processed_messages.mark_done(message_id, response_text=response)
                        phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                        total_ms = (time_module.perf_counter() - t0) * 1000
                        return TurnResult(
                            response_text=response,
                            status_code=200,
                            outcome="flow_cancelled",
                            fast_path=True,
                            stage_from=initial_stage,
                            stage_to=stage,
                            phase1_lease_ms=phase1_ms,
                            phase2_llm_ms=phase2_llm_ms,
                            phase3_state_ms=phase3_ms,
                            total_duration_ms=total_ms,
                        )
                else:
                    # Candidate discarded because locked state was not AWAITING_CONFIRMATION
                    needs_llm_outside_lock = True

        # If fast-path candidate was discarded, parse intent outside of lock
        if needs_llm_outside_lock:
            t_llm1 = time_module.perf_counter()
            try:
                intent = await self._intent_parser.parse(user_message=text)
                phase2_llm_ms += (time_module.perf_counter() - t_llm1) * 1000
            except LLMInvalidOutputError:
                phase2_llm_ms += (time_module.perf_counter() - t_llm1) * 1000
                response = render_template(TemplateKey.CLARIFICATION_PROMPT)
                async with self._uow_factory() as uow:
                    await uow.processed_messages.mark_done(message_id, response_text=response)
                phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                total_ms = (time_module.perf_counter() - t0) * 1000
                return TurnResult(
                    response_text=response,
                    status_code=200,
                    outcome="llm_clarification",
                    fast_path=False,
                    stage_from=initial_stage,
                    stage_to=stage,
                    phase1_lease_ms=phase1_ms,
                    phase2_llm_ms=phase2_llm_ms,
                    phase3_state_ms=phase3_ms,
                    total_duration_ms=total_ms,
                )

        # If not EXECUTING, resolve state machine transition inside short Tx
        if stage != ConversationStage.EXECUTING.value:
            async with self._uow_factory() as uow:
                # Re-lock row to apply transition
                try:
                    state = await uow.conversation_states.get(phone, for_update=True)
                except InvalidConversationStateError as exc:
                    return await self._handle_invalid_conversation_state(
                        uow=uow,
                        phone=phone,
                        message_id=message_id,
                        error=exc,
                        t3_start=t3_start,
                        t0=t0,
                        phase1_ms=phase1_ms,
                        phase2_llm_ms=phase2_llm_ms,
                        initial_stage=initial_stage,
                    )

                if state is not None:
                    stage = state.stage.value
                    slots = slots_to_json(state.slots)
                customer = await uow.customers.get_by_phone(phone)
                if customer is not None:
                    slots["customer_name"] = customer.name

                stage, slots, response = await self._transition_state(
                    uow, phone, stage, slots, text, intent
                )
                await self._save_state(uow, phone, stage, slots)

            # If transition didn't move to EXECUTING, finalize message
            if stage != ConversationStage.EXECUTING.value:
                final_response = response or render_template(TemplateKey.GREETING_HELP)
                async with self._uow_factory() as uow:
                    await uow.processed_messages.mark_done(message_id, response_text=final_response)
                phase3_ms = (time_module.perf_counter() - t3_start) * 1000
                total_ms = (time_module.perf_counter() - t0) * 1000
                return TurnResult(
                    response_text=final_response,
                    status_code=200,
                    outcome="turn_progressed",
                    fast_path=(fast_path_candidate is not None and not needs_llm_outside_lock),
                    stage_from=initial_stage,
                    stage_to=stage,
                    phase1_lease_ms=phase1_ms,
                    phase2_llm_ms=phase2_llm_ms,
                    phase3_state_ms=phase3_ms,
                    total_duration_ms=total_ms,
                )

        phase3_ms = (time_module.perf_counter() - t3_start) * 1000

        # -------------------------------------------------------------
        # Phase 4 & 5: Action Execution & Post-Action State Update
        # -------------------------------------------------------------
        t4_start = time_module.perf_counter()
        flow = self._get_required_flow(stage, slots, phone)
        if flow is None:
            async with self._uow_factory() as uow:
                await self._save_state(
                    uow,
                    phone=phone,
                    stage=ConversationStage.IDLE.value,
                    slots={},
                )
                response = render_template(TemplateKey.GREETING_HELP)
                await uow.processed_messages.mark_done(message_id, response_text=response)
            total_ms = (time_module.perf_counter() - t0) * 1000
            return TurnResult(
                response_text=response,
                status_code=200,
                outcome="missing_pending_flow",
                total_duration_ms=total_ms,
            )

        try:
            details = ConfirmationDetails.from_slots(slots, self._business_tz)
            if flow == ConversationFlow.RESCHEDULE and not details.old_date_time:
                raise MissingConfirmationDataError(("old_date_time",))
        except MissingConfirmationDataError as exc:
            stage, slots, response = self._handle_missing_confirmation_data(phone, slots, exc)
            async with self._uow_factory() as uow:
                await self._save_state(uow, phone, stage, slots)
                await uow.processed_messages.mark_done(message_id, response_text=response)
            total_ms = (time_module.perf_counter() - t0) * 1000
            return TurnResult(
                response_text=response,
                status_code=200,
                outcome="missing_confirmation_data",
                total_duration_ms=total_ms,
            )

        action_id_str = slots.get("action_id")
        action_id = uuid.UUID(action_id_str) if action_id_str else None
        outcome = "ok"

        if flow == ConversationFlow.BOOK:
            service_id = uuid.UUID(slots["service_id"])
            barber_id = _barber_filter(slots)
            target_date = date.fromisoformat(slots["local_date"])
            target_time = time.fromisoformat(slots["local_time"])
            starts_at = localize_datetime(target_date, target_time, self._business_tz)
            customer_name = slots.get("customer_name", "Cliente")

            try:
                booking_res = await self._booking_service.book(
                    customer_phone=phone,
                    customer_name=customer_name,
                    service_id=service_id,
                    starts_at=starts_at,
                    barber_id=barber_id,
                    action_id=action_id,
                )
                barber_display = slots.get("barber_name") or "um de nossos profissionais"
                if booking_res.appointment.barber_id:
                    async with self._uow_factory() as uow:
                        assigned_barber = await uow.barbers.get_by_id(
                            booking_res.appointment.barber_id
                        )
                        if assigned_barber:
                            barber_display = assigned_barber.name

                async with self._uow_factory() as uow:
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.IDLE.value,
                        slots={},
                    )
                stage = ConversationStage.IDLE.value
                outcome = "booking_confirmed"
                response = render_template(
                    TemplateKey.BOOKING_SUCCESS,
                    name=customer_name,
                    service=slots.get("service_name", "Corte"),
                    date=format_short_date_pt_br(target_date),
                    time=format_time_pt_br(target_time),
                    barber=barber_display,
                )
            except SlotUnavailableError as exc:
                outcome = "slot_conflict"
                alternatives = exc.alternatives
                alternatives_iso = [dt.isoformat() for dt in alternatives]
                async with self._uow_factory() as uow:
                    slots["alternative_slots"] = alternatives_iso
                    slots.pop("action_id", None)
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.OFFERING_ALTERNATIVES.value,
                        slots=slots,
                    )
                stage = ConversationStage.OFFERING_ALTERNATIVES.value
                if alternatives:
                    formatted_alts = ", ".join(
                        format_time_pt_br(dt.astimezone(self._business_tz).time())
                        for dt in alternatives[:3]
                    )
                    response = render_template(
                        TemplateKey.OFFER_ALTERNATIVES,
                        requested_time=format_time_pt_br(target_time),
                        alternatives_list=formatted_alts,
                    )
                else:
                    response = render_template(TemplateKey.NO_SLOTS_AVAILABLE)

        elif flow == ConversationFlow.RESCHEDULE:
            appointment_id = uuid.UUID(slots["appointment_id"])
            target_date = date.fromisoformat(slots["local_date"])
            target_time = time.fromisoformat(slots["local_time"])
            new_starts_at = localize_datetime(target_date, target_time, self._business_tz)

            try:
                res_result = await self._booking_service.reschedule(
                    appointment_id=appointment_id,
                    customer_phone=phone,
                    new_starts_at=new_starts_at,
                    action_id=action_id,
                )
                barber_name = slots.get("barber_name", "seu profissional")
                if res_result.appointment.barber_id:
                    async with self._uow_factory() as uow:
                        assigned_barber = await uow.barbers.get_by_id(
                            res_result.appointment.barber_id
                        )
                        if assigned_barber:
                            barber_name = assigned_barber.name

                async with self._uow_factory() as uow:
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.IDLE.value,
                        slots={},
                    )
                stage = ConversationStage.IDLE.value
                outcome = "reschedule_confirmed"
                response = render_template(
                    TemplateKey.RESCHEDULE_SUCCESS,
                    new_date=format_short_date_pt_br(target_date),
                    new_time=format_time_pt_br(target_time),
                    barber=barber_name,
                )
            except SlotUnavailableError as exc:
                outcome = "slot_conflict"
                alternatives = exc.alternatives
                alternatives_iso = [dt.isoformat() for dt in alternatives]
                async with self._uow_factory() as uow:
                    slots["alternative_slots"] = alternatives_iso
                    slots.pop("action_id", None)
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.OFFERING_ALTERNATIVES.value,
                        slots=slots,
                    )
                stage = ConversationStage.OFFERING_ALTERNATIVES.value
                if alternatives:
                    formatted_alts = ", ".join(
                        format_time_pt_br(dt.astimezone(self._business_tz).time())
                        for dt in alternatives[:3]
                    )
                    response = render_template(
                        TemplateKey.OFFER_ALTERNATIVES,
                        requested_time=format_time_pt_br(target_time),
                        alternatives_list=formatted_alts,
                    )
                else:
                    response = render_template(TemplateKey.NO_SLOTS_AVAILABLE)

        elif flow == ConversationFlow.CANCEL:
            appointment_id = uuid.UUID(slots["appointment_id"])
            cancel_result = await self._booking_service.cancel(
                appointment_id=appointment_id,
                customer_phone=phone,
                action_id=action_id,
            )
            async with self._uow_factory() as uow:
                await self._save_state(
                    uow,
                    phone=phone,
                    stage=ConversationStage.IDLE.value,
                    slots={},
                )
            stage = ConversationStage.IDLE.value
            outcome = "cancel_confirmed"
            cancel_date = (
                date.fromisoformat(slots["local_date"])
                if "local_date" in slots
                else cancel_result.appointment.starts_at.astimezone(self._business_tz).date()
            )
            cancel_time = (
                time.fromisoformat(slots["local_time"])
                if "local_time" in slots
                else cancel_result.appointment.starts_at.astimezone(self._business_tz).time()
            )
            response = render_template(
                TemplateKey.CANCEL_SUCCESS,
                service=slots.get("service_name", "serviço"),
                date=format_short_date_pt_br(cancel_date),
                time=format_time_pt_br(cancel_time),
            )
        else:
            response = render_template(TemplateKey.GREETING_HELP)

        phase4_ms = (time_module.perf_counter() - t4_start) * 1000

        # -------------------------------------------------------------
        # Phase 6: Finalize & Persist Response Text (Short Tx)
        # -------------------------------------------------------------
        async with self._uow_factory() as uow:
            await uow.processed_messages.mark_done(message_id, response_text=response)

        total_ms = (time_module.perf_counter() - t0) * 1000
        return TurnResult(
            response_text=response,
            status_code=200,
            outcome=outcome,
            fast_path=(fast_path_candidate is not None and not needs_llm_outside_lock),
            stage_from=initial_stage,
            stage_to=stage,
            phase1_lease_ms=phase1_ms,
            phase2_llm_ms=phase2_llm_ms,
            phase3_state_ms=phase3_ms,
            phase4_action_ms=phase4_ms,
            total_duration_ms=total_ms,
        )

    async def _transition_state(
        self,
        uow: UnitOfWork,
        phone: str,
        stage: str,
        slots: dict[str, Any],
        text: str,
        intent: BookingIntent | None,
    ) -> tuple[str, dict[str, Any], str]:
        """Compute state machine transition and render template."""
        normalized_msg = normalize_text(text)

        # 1. State: CHOOSING_APPOINTMENT
        if stage == ConversationStage.CHOOSING_APPOINTMENT.value:
            flow = self._get_required_flow(stage, slots, phone)
            if flow is None:
                return (
                    ConversationStage.IDLE.value,
                    {},
                    render_template(TemplateKey.GREETING_HELP),
                )
            slots["flow"] = flow.value
            if normalized_msg in _NEGATIVE_TOKENS:
                response = render_flow_cancelled(flow)
                return (
                    ConversationStage.IDLE.value,
                    {},
                    response,
                )
            candidates = slots.get("candidate_appointment_ids", [])
            selected_idx = _extract_selection_index(text, len(candidates))
            if selected_idx is not None:
                appointment_id = uuid.UUID(candidates[selected_idx])
                slots["appointment_id"] = str(appointment_id)
                slots.pop("candidate_appointment_ids", None)
                appt = await uow.appointments.get_by_id(appointment_id)
                if appt is None or appt.status != AppointmentStatus.CONFIRMED.value:
                    return (
                        ConversationStage.IDLE.value,
                        {},
                        render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
                    )

                service = await uow.services.get_by_id(appt.service_id)
                barber = await uow.barbers.get_by_id(appt.barber_id)
                cust = await uow.customers.get_by_id(appt.customer_id)
                slots["service_id"] = str(appt.service_id)
                slots["barber_id"] = str(appt.barber_id)
                if service:
                    slots["service_name"] = service.name
                    slots["service_price_cents"] = service.price_cents
                if barber:
                    slots["barber_name"] = barber.name
                if cust:
                    slots["customer_name"] = cust.name
                local_start = appt.starts_at.astimezone(self._business_tz)
                slots["local_date"] = local_start.date().isoformat()
                slots["local_time"] = local_start.time().isoformat()
                old_dt_str = (
                    f"{format_short_date_pt_br(local_start.date())} às "
                    f"{format_time_pt_br(local_start.time())}"
                )
                slots["old_date_time"] = old_dt_str

                if flow == ConversationFlow.CANCEL:
                    try:
                        details = ConfirmationDetails.from_slots(slots, self._business_tz)
                        return self._enter_confirmation(
                            flow=ConversationFlow.CANCEL,
                            slots=slots,
                            details=details,
                        )
                    except MissingConfirmationDataError as exc:
                        return self._handle_missing_confirmation_data(phone, slots, exc)
                else:  # reschedule
                    if intent and intent.local_date and intent.local_time:
                        slots["local_date"] = intent.local_date.isoformat()
                        slots["local_time"] = intent.local_time.isoformat()
                        try:
                            details = ConfirmationDetails.from_slots(slots, self._business_tz)
                            return self._enter_confirmation(
                                flow=ConversationFlow.RESCHEDULE,
                                slots=slots,
                                details=details,
                            )
                        except MissingConfirmationDataError as exc:
                            return self._handle_missing_confirmation_data(phone, slots, exc)
                    return (
                        ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                        slots,
                        render_template(TemplateKey.ASK_RESCHEDULE_DATETIME),
                    )

            # Re-prompt appointment list
            candidate_appts = [
                await uow.appointments.get_by_id(uuid.UUID(aid)) for aid in candidates
            ]
            appt_lines: list[str] = []
            for i, a in enumerate(candidate_appts):
                if a:
                    svc = await uow.services.get_by_id(a.service_id)
                    brb = await uow.barbers.get_by_id(a.barber_id)
                    s_name = svc.name if svc else "Serviço"
                    b_name = brb.name if brb else "Profissional"
                    dt_local = a.starts_at.astimezone(self._business_tz)
                    appt_lines.append(
                        f"{i + 1}. {s_name} em {format_short_date_pt_br(dt_local.date())} "
                        f"às {format_time_pt_br(dt_local.time())} com {b_name}"
                    )
            return (
                ConversationStage.CHOOSING_APPOINTMENT.value,
                slots,
                render_template(
                    TemplateKey.CHOOSE_APPOINTMENT,
                    appointments_list="\n".join(appt_lines),
                ),
            )

        # 2. State: OFFERING_ALTERNATIVES
        if stage == ConversationStage.OFFERING_ALTERNATIVES.value:
            flow = self._get_required_flow(stage, slots, phone)
            if flow is None:
                return (
                    ConversationStage.IDLE.value,
                    {},
                    render_template(TemplateKey.GREETING_HELP),
                )
            slots["flow"] = flow.value
            if normalized_msg in _NEGATIVE_TOKENS:
                response = render_flow_cancelled(flow)
                return (
                    ConversationStage.IDLE.value,
                    {},
                    response,
                )
            alts = slots.get("alternative_slots", [])
            chosen_idx = _extract_alternative_index(text, alts)
            if chosen_idx is not None:
                chosen_dt = datetime.fromisoformat(alts[chosen_idx]).astimezone(self._business_tz)
                slots["local_date"] = chosen_dt.date().isoformat()
                slots["local_time"] = chosen_dt.time().isoformat()
                slots.pop("alternative_slots", None)
                if flow == ConversationFlow.RESCHEDULE and (
                    "old_date_time" not in slots or "service_price_cents" not in slots
                ):
                    origin = await self._load_reschedule_origin(uow, phone, slots)
                    if origin is None:
                        slots.clear()
                        return (
                            ConversationStage.IDLE.value,
                            slots,
                            render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                        )
                try:
                    details = ConfirmationDetails.from_slots(slots, self._business_tz)
                    return self._enter_confirmation(
                        flow=flow,
                        slots=slots,
                        details=details,
                    )
                except MissingConfirmationDataError as exc:
                    return self._handle_missing_confirmation_data(phone, slots, exc)

            if intent and (intent.local_date or intent.local_time):
                if intent.local_date:
                    slots["local_date"] = intent.local_date.isoformat()
                if intent.local_time:
                    slots["local_time"] = intent.local_time.isoformat()
                slots.pop("alternative_slots", None)
                return await self._validate_booking_slots_and_confirm(uow, phone, slots, flow=flow)

            return (
                ConversationStage.OFFERING_ALTERNATIVES.value,
                slots,
                render_template(TemplateKey.CLARIFICATION_PROMPT),
            )

        # 3. State: AWAITING_CONFIRMATION (User replied with non-exact tokens)
        if stage == ConversationStage.AWAITING_CONFIRMATION.value:
            pending_flow = self._get_required_flow(stage, slots, phone)
            if pending_flow is None:
                return (
                    ConversationStage.IDLE.value,
                    {},
                    render_template(TemplateKey.GREETING_HELP),
                )
            slots["flow"] = pending_flow.value
            candidate = classify_fast_path_candidate(text)
            if candidate is not None:
                is_affirmative = resolve_confirmation(candidate, pending_flow)
                if is_affirmative:
                    try:
                        details = ConfirmationDetails.from_slots(slots, self._business_tz)
                        if (
                            pending_flow == ConversationFlow.RESCHEDULE
                            and not details.old_date_time
                        ):
                            raise MissingConfirmationDataError(("old_date_time",))
                    except MissingConfirmationDataError as exc:
                        return self._handle_missing_confirmation_data(phone, slots, exc)
                    slots.pop("executing_message_id", None)
                    return (
                        ConversationStage.EXECUTING.value,
                        slots,
                        "",
                    )
                response = render_flow_cancelled(pending_flow)
                return (
                    ConversationStage.IDLE.value,
                    {},
                    response,
                )
            if intent:
                has_updates = False
                active_services = await uow.services.list_active()
                active_barbers = await uow.barbers.list_active()

                if intent.service_name:
                    res_s = self._entity_resolver.resolve_service(
                        intent.service_name, active_services
                    )
                    if res_s.matched and res_s.entity:
                        slots["service_id"] = str(res_s.entity.id)
                        slots["service_name"] = res_s.entity.name
                        slots["service_price_cents"] = res_s.entity.price_cents
                        has_updates = True

                if intent.barber_name:
                    res_b = self._entity_resolver.resolve_barber(intent.barber_name, active_barbers)
                    if res_b.matched:
                        if res_b.entity is None:
                            slots["barber_id"] = ANY_BARBER
                            slots["barber_name"] = None
                        else:
                            slots["barber_id"] = str(res_b.entity.id)
                            slots["barber_name"] = res_b.entity.name
                        has_updates = True

                if intent.local_date:
                    slots["local_date"] = intent.local_date.isoformat()
                    has_updates = True
                if intent.local_time:
                    slots["local_time"] = intent.local_time.isoformat()
                    has_updates = True

                if has_updates:
                    # Invalidate previous action_id with fresh uuid
                    slots.pop("action_id", None)
                    match pending_flow:
                        case ConversationFlow.BOOK:
                            return await self._validate_booking_slots_and_confirm(
                                uow, phone, slots, flow=ConversationFlow.BOOK
                            )
                        case ConversationFlow.RESCHEDULE:
                            origin = await self._load_reschedule_origin(uow, phone, slots)
                            if origin is None:
                                slots.clear()
                                return (
                                    ConversationStage.IDLE.value,
                                    slots,
                                    render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                                )
                            return await self._validate_booking_slots_and_confirm(
                                uow, phone, slots, flow=ConversationFlow.RESCHEDULE
                            )
                        case ConversationFlow.CANCEL:
                            origin = await self._load_reschedule_origin(uow, phone, slots)
                            if origin is None:
                                slots.clear()
                                return (
                                    ConversationStage.IDLE.value,
                                    slots,
                                    render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                                )
                            return await self._validate_booking_slots_and_confirm(
                                uow, phone, slots, flow=ConversationFlow.RESCHEDULE
                            )
                        case _ as unreachable:
                            assert_never(unreachable)

            # Re-present confirmation prompt if user input was unclear
            if pending_flow == ConversationFlow.BOOK:
                try:
                    details = ConfirmationDetails.from_slots(slots, self._business_tz)
                    return self._enter_confirmation(
                        flow=pending_flow,
                        slots=slots,
                        details=details,
                    )
                except MissingConfirmationDataError as exc:
                    return self._handle_missing_confirmation_data(phone, slots, exc)

            if pending_flow in (ConversationFlow.RESCHEDULE, ConversationFlow.CANCEL):
                if "appointment_id" in slots and (
                    "old_date_time" not in slots
                    or "service_price_cents" not in slots
                    or "customer_name" not in slots
                ):
                    await self._load_reschedule_origin(uow, phone, slots)
                try:
                    ConfirmationDetails.from_slots(slots, self._business_tz)
                    if pending_flow == ConversationFlow.RESCHEDULE and not slots.get(
                        "old_date_time"
                    ):
                        raise MissingConfirmationDataError(("old_date_time",))
                except MissingConfirmationDataError as exc:
                    return self._handle_missing_confirmation_data(phone, slots, exc)

            return (
                stage,
                slots,
                render_template(TemplateKey.CLARIFICATION_PROMPT),
            )

        # 4. State: IDLE - Detect Intent
        if stage == ConversationStage.IDLE.value:
            if intent is None:
                return (
                    ConversationStage.IDLE.value,
                    {},
                    render_template(TemplateKey.GREETING_HELP),
                )

            if intent.intent == IntentType.CANCEL or intent.intent == IntentType.RESCHEDULE:
                flow = (
                    ConversationFlow.CANCEL
                    if intent.intent == IntentType.CANCEL
                    else ConversationFlow.RESCHEDULE
                )
                slots["flow"] = flow.value
                customer = await uow.customers.get_by_phone(phone)
                if customer is None:
                    return (
                        ConversationStage.IDLE.value,
                        {},
                        render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
                    )
                appts = await uow.appointments.list_by_customer(customer.id)
                now_utc = self._clock.now()
                future_appts = [
                    a
                    for a in appts
                    if a.status == AppointmentStatus.CONFIRMED.value and a.starts_at > now_utc
                ]
                if not future_appts:
                    return (
                        ConversationStage.IDLE.value,
                        {},
                        render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
                    )

                if len(future_appts) == 1:
                    target_appt = future_appts[0]
                    slots["appointment_id"] = str(target_appt.id)
                    service = await uow.services.get_by_id(target_appt.service_id)
                    barber = await uow.barbers.get_by_id(target_appt.barber_id)
                    slots["service_id"] = str(target_appt.service_id)
                    slots["barber_id"] = str(target_appt.barber_id)
                    if service:
                        slots["service_name"] = service.name
                        slots["service_price_cents"] = service.price_cents
                    if barber:
                        slots["barber_name"] = barber.name
                    slots["customer_name"] = customer.name
                    start_local = target_appt.starts_at.astimezone(self._business_tz)
                    slots["local_date"] = start_local.date().isoformat()
                    slots["local_time"] = start_local.time().isoformat()
                    old_dt_str = (
                        f"{format_short_date_pt_br(start_local.date())} às "
                        f"{format_time_pt_br(start_local.time())}"
                    )
                    slots["old_date_time"] = old_dt_str

                    if flow == ConversationFlow.CANCEL:
                        try:
                            details = ConfirmationDetails.from_slots(slots, self._business_tz)
                            return self._enter_confirmation(
                                flow=ConversationFlow.CANCEL,
                                slots=slots,
                                details=details,
                            )
                        except MissingConfirmationDataError as exc:
                            return self._handle_missing_confirmation_data(phone, slots, exc)
                    else:  # reschedule
                        if intent.local_date and intent.local_time:
                            slots["local_date"] = intent.local_date.isoformat()
                            slots["local_time"] = intent.local_time.isoformat()
                            try:
                                details = ConfirmationDetails.from_slots(slots, self._business_tz)
                                return self._enter_confirmation(
                                    flow=ConversationFlow.RESCHEDULE,
                                    slots=slots,
                                    details=details,
                                )
                            except MissingConfirmationDataError as exc:
                                return self._handle_missing_confirmation_data(phone, slots, exc)
                        return (
                            ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                            slots,
                            render_template(TemplateKey.ASK_RESCHEDULE_DATETIME),
                        )

                # Multiple appointments -> CHOOSING_APPOINTMENT
                slots["candidate_appointment_ids"] = [str(a.id) for a in future_appts]
                appt_lines = []
                for i, a in enumerate(future_appts):
                    svc = await uow.services.get_by_id(a.service_id)
                    brb = await uow.barbers.get_by_id(a.barber_id)
                    s_name = svc.name if svc else "Serviço"
                    b_name = brb.name if brb else "Profissional"
                    dt_local = a.starts_at.astimezone(self._business_tz)
                    appt_lines.append(
                        f"{i + 1}. {s_name} em {format_short_date_pt_br(dt_local.date())} "
                        f"às {format_time_pt_br(dt_local.time())} com {b_name}"
                    )
                return (
                    ConversationStage.CHOOSING_APPOINTMENT.value,
                    slots,
                    render_template(
                        TemplateKey.CHOOSE_APPOINTMENT,
                        appointments_list="\n".join(appt_lines),
                    ),
                )

            elif intent.intent == IntentType.BOOK:
                slots["flow"] = ConversationFlow.BOOK.value
                stage = ConversationStage.COLLECTING_BOOKING_DETAILS.value
            else:
                return (
                    ConversationStage.IDLE.value,
                    {},
                    render_template(TemplateKey.GREETING_HELP),
                )

        # 5. State: COLLECTING_BOOKING_DETAILS
        flow = self._get_required_flow(stage, slots, phone)
        if flow is None:
            return (
                ConversationStage.IDLE.value,
                {},
                render_template(TemplateKey.GREETING_HELP),
            )
        slots["flow"] = flow.value
        if flow == ConversationFlow.RESCHEDULE:
            if intent and intent.local_date:
                slots["local_date"] = intent.local_date.isoformat()
            if intent and intent.local_time:
                slots["local_time"] = intent.local_time.isoformat()

            if "local_date" in slots and "local_time" in slots:
                if "old_date_time" not in slots or "service_price_cents" not in slots:
                    origin = await self._load_reschedule_origin(uow, phone, slots)
                    if origin is None:
                        slots.clear()
                        return (
                            ConversationStage.IDLE.value,
                            slots,
                            render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                        )
                try:
                    details = ConfirmationDetails.from_slots(slots, self._business_tz)
                    return self._enter_confirmation(
                        flow=ConversationFlow.RESCHEDULE,
                        slots=slots,
                        details=details,
                    )
                except MissingConfirmationDataError as exc:
                    return self._handle_missing_confirmation_data(phone, slots, exc)
            return (
                ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                slots,
                render_template(TemplateKey.ASK_RESCHEDULE_DATETIME),
            )

        # Flow == ConversationFlow.BOOK: merge slots
        active_services = await uow.services.list_active()
        active_barbers = await uow.barbers.list_active()

        # Service resolution
        svc_query = intent.service_name if (intent and intent.service_name) else None
        if svc_query is None and "service_id" not in slots:
            # Check if user sent service name directly as free text
            res_direct = self._entity_resolver.resolve_service(text, active_services)
            if res_direct.matched and res_direct.entity:
                svc_query = text

        if svc_query:
            res_s = self._entity_resolver.resolve_service(svc_query, active_services)
            if res_s.matched and res_s.entity:
                slots["service_id"] = str(res_s.entity.id)
                slots["service_name"] = res_s.entity.name
                slots["service_price_cents"] = res_s.entity.price_cents
            else:
                svc_list = ", ".join(
                    f"{s.name} ({format_currency_brl(s.price_cents)})" for s in active_services
                )
                return (
                    ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                    slots,
                    render_template(TemplateKey.SERVICE_NOT_FOUND, services_list=svc_list),
                )

        # Barber resolution
        barber_query = intent.barber_name if (intent and intent.barber_name) else None
        if barber_query:
            res_b = self._entity_resolver.resolve_barber(barber_query, active_barbers)
            if res_b.matched:
                if res_b.entity is None:
                    slots["barber_id"] = ANY_BARBER
                    slots["barber_name"] = None
                else:
                    slots["barber_id"] = str(res_b.entity.id)
                    slots["barber_name"] = res_b.entity.name
            else:
                barber_list = ", ".join(b.name for b in active_barbers)
                return (
                    ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                    slots,
                    render_template(
                        TemplateKey.BARBER_NOT_FOUND,
                        barbers_list=barber_list,
                    ),
                )

        # Date & Time extraction
        if intent and intent.local_date:
            slots["local_date"] = intent.local_date.isoformat()
        if intent and intent.local_time:
            slots["local_time"] = intent.local_time.isoformat()

        # Customer name extraction: ONLY when awaiting customer_name
        if slots.get("awaiting") == "customer_name":
            raw_candidate = (
                intent.customer_name if (intent and intent.customer_name) else text.strip()
            )
            if is_valid_customer_name(raw_candidate):
                slots["customer_name"] = raw_candidate
                slots.pop("awaiting", None)

        # Check missing slots in order
        if "service_id" not in slots:
            slots["awaiting"] = "service"
            svc_list = ", ".join(
                f"{s.name} ({format_currency_brl(s.price_cents)})" for s in active_services
            )
            return (
                ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                slots,
                render_template(TemplateKey.ASK_SERVICE, services_list=svc_list),
            )

        if "local_date" not in slots or "local_time" not in slots:
            slots["awaiting"] = "date_time"
            return (
                ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                slots,
                render_template(
                    TemplateKey.ASK_DATE_TIME,
                    service_name=slots.get("service_name", "serviço"),
                ),
            )

        if "customer_name" not in slots:
            slots["awaiting"] = "customer_name"
            return (
                ConversationStage.COLLECTING_BOOKING_DETAILS.value,
                slots,
                render_template(TemplateKey.ASK_CUSTOMER_NAME),
            )

        # All slots collected! Check availability and advance to AWAITING_CONFIRMATION
        return await self._validate_booking_slots_and_confirm(uow, phone, slots)

    async def _validate_booking_slots_and_confirm(
        self,
        uow: UnitOfWork,
        phone: str,
        slots: dict[str, Any],
        flow: ConversationFlow = ConversationFlow.BOOK,
    ) -> tuple[str, dict[str, Any], str]:
        """Validate availability for collected slots and produce confirmation prompt."""
        slots["flow"] = flow.value
        service_id = uuid.UUID(slots["service_id"])
        target_date = date.fromisoformat(slots["local_date"])
        target_time = time.fromisoformat(slots["local_time"])
        barber_id = _barber_filter(slots)

        avail = await self._booking_service.list_availability(
            service_id=service_id,
            target_date=target_date,
            barber_id=barber_id,
        )
        requested_dt = localize_datetime(target_date, target_time, self._business_tz)

        # Check if requested slot is available
        is_available = any(s == requested_dt for s in avail.slots)

        if is_available:
            try:
                details = ConfirmationDetails.from_slots(slots, self._business_tz)
                return self._enter_confirmation(
                    flow=flow,
                    slots=slots,
                    details=details,
                )
            except MissingConfirmationDataError as exc:
                return self._handle_missing_confirmation_data(phone, slots, exc)

        # Slot occupied: propose alternatives
        if avail.slots:
            slots.pop("awaiting", None)
            slots["alternative_slots"] = [dt.isoformat() for dt in avail.slots[:3]]
            slots["flow"] = flow.value
            formatted_alts = ", ".join(
                format_time_pt_br(dt.astimezone(self._business_tz).time()) for dt in avail.slots[:3]
            )
            next_stage = ConversationStage.OFFERING_ALTERNATIVES.value
            resp = render_template(
                TemplateKey.OFFER_ALTERNATIVES,
                requested_time=format_time_pt_br(target_time),
                alternatives_list=formatted_alts,
            )
            return next_stage, slots, resp

        # No open slots on that day
        return (
            ConversationStage.COLLECTING_BOOKING_DETAILS.value,
            slots,
            render_template(TemplateKey.NO_SLOTS_AVAILABLE),
        )
