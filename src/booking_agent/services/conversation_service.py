"""ConversationService orchestrating multi-turn chat interactions across 6 phases."""

import logging
import time as time_module
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import assert_never
from zoneinfo import ZoneInfo

from booking_agent.config import Settings
from booking_agent.database.models import Appointment
from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import (
    ANY_BARBER,
    ConversationSlots,
    ConversationSnapshot,
)
from booking_agent.domain.enums import (
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
    format_short_date_pt_br,
    format_time_pt_br,
    render_flow_cancelled,
    render_template,
)
from booking_agent.services.transitions import (
    STAGE_HANDLERS,
    ConversationReads,
    Transition,
    TransitionContext,
    UnhandledStageError,
    UnitOfWorkReads,
    enter_confirmation,
    extract_pending_flow,
    handle_missing_confirmation_data,
    load_reschedule_origin,
    validate_booking_slots_and_confirm,
)
from booking_agent.services.transitions import (
    ConfirmationDetails as ConfirmationDetails,
)
from booking_agent.services.transitions import (
    is_valid_customer_name as is_valid_customer_name,
)
from booking_agent.services.transitions import (
    mask_phone as mask_phone,
)

logger = logging.getLogger(__name__)


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


def _barber_filter(slots: ConversationSlots) -> uuid.UUID | None:
    """Extract optional barber UUID filter from slots, returning None for 'any' or unset."""
    raw_id = slots.barber_id
    if not raw_id or raw_id == ANY_BARBER:
        return None
    if isinstance(raw_id, uuid.UUID):
        return raw_id
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
        slots: ConversationSlots,
    ) -> ConversationSnapshot:
        """Save conversation state to repository with typed domain model.

        Args:
            uow: Active unit of work.
            phone: Customer phone number.
            stage: Target conversation stage string.
            slots: Target conversation slots.

        Returns:
            Saved conversation snapshot.
        """
        typed_stage = ConversationStage(stage)
        return await uow.conversation_states.upsert(phone, typed_stage, slots)

    def _enter_confirmation(
        self,
        flow: ConversationFlow,
        slots: ConversationSlots,
        details: ConfirmationDetails,
    ) -> Transition:
        """Single entry point for transitioning the conversation into AWAITING_CONFIRMATION."""
        return enter_confirmation(flow, slots, details)

    def _handle_missing_confirmation_data(
        self,
        phone: str,
        slots: ConversationSlots,
        error: MissingConfirmationDataError,
    ) -> Transition:
        """Handle missing confirmation details: log event without PII and fail safe to IDLE."""
        return handle_missing_confirmation_data(phone, slots, error)

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
                "phone_masked": mask_phone(phone),
            },
        )
        stage = ConversationStage.IDLE.value
        slots = ConversationSlots()
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
        slots: ConversationSlots,
        phone: str,
    ) -> ConversationFlow | None:
        """Extract and validate the pending flow from state slots."""
        return extract_pending_flow(stage, slots, phone)

    # Backward compatibility alias
    _get_required_flow = _extract_pending_flow

    async def _load_reschedule_origin(
        self,
        reads: ConversationReads,
        phone: str,
        slots: ConversationSlots,
    ) -> Appointment | None:
        """Validate and load the confirmed origin appointment for rescheduling."""
        ctx = TransitionContext(reads, self._clock, self._entity_resolver, self._business_tz, phone)
        return await load_reschedule_origin(ctx, slots)

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
        slots: ConversationSlots
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
                slots = ConversationSlots()
                is_expired = False
            else:
                stage = state.stage.value
                initial_stage = stage
                slots = state.slots
                elapsed = self._clock.now() - state.updated_at
                is_expired = elapsed > timedelta(minutes=self._settings.conversation_ttl_minutes)

            # TTL expiration handling
            if is_expired:
                was_in_progress = (
                    stage != ConversationStage.IDLE.value or slots != ConversationSlots()
                )
                stage = ConversationStage.IDLE.value
                slots = ConversationSlots()
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
                if slots.executing_message_id == message_id or is_executing_stuck:
                    # Retry of the crashed message itself OR next message after lease timeout
                    # (stuck EXECUTING): resume execution with the same action_id
                    slots.executing_message_id = message_id
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
                slots.customer_name = customer.name

            # Fast-path revalidation under lock
            if fast_path_candidate is not None:
                if (
                    stage == ConversationStage.AWAITING_CONFIRMATION.value
                    and slots.action_id is not None
                ):
                    validated_flow = self._get_required_flow(stage, slots, phone)
                    if validated_flow is None:
                        stage = ConversationStage.IDLE.value
                        slots = ConversationSlots()
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
                            transition = self._handle_missing_confirmation_data(phone, slots, exc)
                            stage = transition.stage.value
                            slots = transition.slots
                            response = transition.reply
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
                        slots.executing_message_id = message_id
                        await self._save_state(uow, phone, stage, slots)
                        # Lock released upon exiting block; advances to Phase 4
                    else:
                        response = render_flow_cancelled(validated_flow)
                        stage = ConversationStage.IDLE.value
                        slots = ConversationSlots()
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
                    slots = state.slots
                customer = await uow.customers.get_by_phone(phone)
                if customer is not None:
                    slots.customer_name = customer.name

                reads = UnitOfWorkReads(uow, self._booking_service)
                transition = await self._transition_state(reads, phone, stage, slots, text, intent)
                stage = transition.stage.value
                slots = transition.slots
                response = transition.reply
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
            slots = ConversationSlots()
            async with self._uow_factory() as uow:
                await self._save_state(
                    uow,
                    phone=phone,
                    stage=ConversationStage.IDLE.value,
                    slots=slots,
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
            transition = self._handle_missing_confirmation_data(phone, slots, exc)
            stage = transition.stage.value
            slots = transition.slots
            response = transition.reply
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

        action_id = slots.action_id
        outcome = "ok"

        if flow == ConversationFlow.BOOK:
            if slots.service_id is None or slots.local_date is None or slots.local_time is None:
                raise MissingConfirmationDataError(("service_id", "local_date", "local_time"))
            service_id = slots.service_id
            barber_id = _barber_filter(slots)
            target_date = slots.local_date
            target_time = slots.local_time
            starts_at = localize_datetime(target_date, target_time, self._business_tz)
            customer_name = slots.customer_name or "Cliente"
            service_name = slots.service_name or "Corte"

            try:
                booking_res = await self._booking_service.book(
                    customer_phone=phone,
                    customer_name=customer_name,
                    service_id=service_id,
                    starts_at=starts_at,
                    barber_id=barber_id,
                    action_id=action_id,
                )
                barber_display = slots.barber_name or "um de nossos profissionais"
                if booking_res.appointment.barber_id:
                    async with self._uow_factory() as uow:
                        assigned_barber = await uow.barbers.get_by_id(
                            booking_res.appointment.barber_id
                        )
                        if assigned_barber:
                            barber_display = assigned_barber.name

                slots = ConversationSlots()
                async with self._uow_factory() as uow:
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.IDLE.value,
                        slots=slots,
                    )
                stage = ConversationStage.IDLE.value
                outcome = "booking_confirmed"
                response = render_template(
                    TemplateKey.BOOKING_SUCCESS,
                    name=customer_name,
                    service=service_name,
                    date=format_short_date_pt_br(target_date),
                    time=format_time_pt_br(target_time),
                    barber=barber_display,
                )
            except SlotUnavailableError as exc:
                outcome = "slot_conflict"
                alternatives = exc.alternatives
                slots.alternative_slots = alternatives
                slots.action_id = None
                async with self._uow_factory() as uow:
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
            if slots.appointment_id is None or slots.local_date is None or slots.local_time is None:
                raise MissingConfirmationDataError(("appointment_id", "local_date", "local_time"))
            appointment_id = slots.appointment_id
            target_date = slots.local_date
            target_time = slots.local_time
            new_starts_at = localize_datetime(target_date, target_time, self._business_tz)

            try:
                res_result = await self._booking_service.reschedule(
                    appointment_id=appointment_id,
                    customer_phone=phone,
                    new_starts_at=new_starts_at,
                    action_id=action_id,
                )
                barber_name = slots.barber_name or "seu profissional"
                if res_result.appointment.barber_id:
                    async with self._uow_factory() as uow:
                        assigned_barber = await uow.barbers.get_by_id(
                            res_result.appointment.barber_id
                        )
                        if assigned_barber:
                            barber_name = assigned_barber.name

                slots = ConversationSlots()
                async with self._uow_factory() as uow:
                    await self._save_state(
                        uow,
                        phone=phone,
                        stage=ConversationStage.IDLE.value,
                        slots=slots,
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
                slots.alternative_slots = alternatives
                slots.action_id = None
                async with self._uow_factory() as uow:
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
            if slots.appointment_id is None:
                raise MissingConfirmationDataError(("appointment_id",))
            appointment_id = slots.appointment_id
            cancel_result = await self._booking_service.cancel(
                appointment_id=appointment_id,
                customer_phone=phone,
                action_id=action_id,
            )
            cancel_date = (
                slots.local_date
                if slots.local_date is not None
                else cancel_result.appointment.starts_at.astimezone(self._business_tz).date()
            )
            cancel_time = (
                slots.local_time
                if slots.local_time is not None
                else cancel_result.appointment.starts_at.astimezone(self._business_tz).time()
            )
            service_name = slots.service_name or "serviço"
            slots = ConversationSlots()
            async with self._uow_factory() as uow:
                await self._save_state(
                    uow,
                    phone=phone,
                    stage=ConversationStage.IDLE.value,
                    slots=slots,
                )
            stage = ConversationStage.IDLE.value
            outcome = "cancel_confirmed"
            response = render_template(
                TemplateKey.CANCEL_SUCCESS,
                service=service_name,
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
        reads: ConversationReads,
        phone: str,
        stage: str,
        slots: ConversationSlots,
        text: str,
        intent: BookingIntent | None,
    ) -> Transition:
        """Compute state machine transition and render template."""
        try:
            typed_stage = ConversationStage(stage)
        except ValueError:
            typed_stage = None

        ctx = TransitionContext(
            reads=reads,
            clock=self._clock,
            entity_resolver=self._entity_resolver,
            business_tz=self._business_tz,
            phone=phone,
        )

        if typed_stage is not None and typed_stage in STAGE_HANDLERS:
            return await STAGE_HANDLERS[typed_stage](ctx, slots, text, intent)

        # 3. State: AWAITING_CONFIRMATION (User replied with non-exact tokens)
        if stage == ConversationStage.AWAITING_CONFIRMATION.value:
            pending_flow = self._get_required_flow(stage, slots, phone)
            if pending_flow is None:
                return Transition(
                    stage=ConversationStage.IDLE,
                    slots=ConversationSlots(),
                    reply=render_template(TemplateKey.GREETING_HELP),
                )
            slots.flow = pending_flow
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
                    slots.executing_message_id = None
                    return Transition(
                        stage=ConversationStage.EXECUTING,
                        slots=slots,
                        reply="",
                    )
                response = render_flow_cancelled(pending_flow)
                return Transition(
                    stage=ConversationStage.IDLE,
                    slots=ConversationSlots(),
                    reply=response,
                )
            if intent:
                has_updates = False
                active_services = await reads.list_active_services()
                active_barbers = await reads.list_active_barbers()

                if intent.service_name:
                    res_s = self._entity_resolver.resolve_service(
                        intent.service_name, active_services
                    )
                    if res_s.matched and res_s.entity:
                        slots.service_id = res_s.entity.id
                        slots.service_name = res_s.entity.name
                        slots.service_price_cents = res_s.entity.price_cents
                        has_updates = True

                if intent.barber_name:
                    res_b = self._entity_resolver.resolve_barber(intent.barber_name, active_barbers)
                    if res_b.matched:
                        if res_b.entity is None:
                            slots.barber_id = ANY_BARBER
                            slots.barber_name = None
                        else:
                            slots.barber_id = res_b.entity.id
                            slots.barber_name = res_b.entity.name
                        has_updates = True

                if intent.local_date:
                    slots.local_date = intent.local_date
                    has_updates = True
                if intent.local_time:
                    slots.local_time = intent.local_time
                    has_updates = True

                if has_updates:
                    # Invalidate previous action_id with fresh uuid
                    slots.action_id = None
                    match pending_flow:
                        case ConversationFlow.BOOK:
                            return await self._validate_booking_slots_and_confirm(
                                reads, phone, slots, flow=ConversationFlow.BOOK
                            )
                        case ConversationFlow.RESCHEDULE:
                            origin = await self._load_reschedule_origin(reads, phone, slots)
                            if origin is None:
                                slots = ConversationSlots()
                                return Transition(
                                    stage=ConversationStage.IDLE,
                                    slots=slots,
                                    reply=render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                                )
                            return await self._validate_booking_slots_and_confirm(
                                reads, phone, slots, flow=ConversationFlow.RESCHEDULE
                            )
                        case ConversationFlow.CANCEL:
                            origin = await self._load_reschedule_origin(reads, phone, slots)
                            if origin is None:
                                slots = ConversationSlots()
                                return Transition(
                                    stage=ConversationStage.IDLE,
                                    slots=slots,
                                    reply=render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                                )
                            return await self._validate_booking_slots_and_confirm(
                                reads, phone, slots, flow=ConversationFlow.RESCHEDULE
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
                if slots.appointment_id is not None and (
                    slots.old_date_time is None
                    or slots.service_price_cents is None
                    or slots.customer_name is None
                ):
                    await self._load_reschedule_origin(reads, phone, slots)
                try:
                    ConfirmationDetails.from_slots(slots, self._business_tz)
                    if pending_flow == ConversationFlow.RESCHEDULE and not slots.old_date_time:
                        raise MissingConfirmationDataError(("old_date_time",))
                except MissingConfirmationDataError as exc:
                    return self._handle_missing_confirmation_data(phone, slots, exc)

            return Transition(
                stage=ConversationStage(stage),
                slots=slots,
                reply=render_template(TemplateKey.CLARIFICATION_PROMPT),
            )

        raise UnhandledStageError(stage)

    async def _validate_booking_slots_and_confirm(
        self,
        reads: ConversationReads,
        phone: str,
        slots: ConversationSlots,
        flow: ConversationFlow = ConversationFlow.BOOK,
    ) -> Transition:
        """Validate availability for collected slots and produce confirmation prompt."""
        ctx = TransitionContext(reads, self._clock, self._entity_resolver, self._business_tz, phone)
        return await validate_booking_slots_and_confirm(ctx, slots, flow=flow)
