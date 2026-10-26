"""Handler for COLLECTING_BOOKING_DETAILS stage transitions."""

from __future__ import annotations

import re

from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ANY_BARBER, ConversationSlots
from booking_agent.domain.enums import (
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.entity_resolver import normalize_text
from booking_agent.services.templates import (
    TemplateKey,
    format_currency_brl,
    render_template,
)
from booking_agent.services.transitions.confirmation import (
    ConfirmationDetails,
    enter_confirmation,
    extract_pending_flow,
    handle_missing_confirmation_data,
    load_reschedule_origin,
    validate_booking_slots_and_confirm,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.transition import Transition

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


async def handle_collecting_booking_details(
    ctx: TransitionContext,
    slots: ConversationSlots,
    text: str,
    intent: BookingIntent | None,
) -> Transition:
    """Handle state transitions when in COLLECTING_BOOKING_DETAILS."""
    stage = ConversationStage.COLLECTING_BOOKING_DETAILS.value
    flow = extract_pending_flow(stage, slots, ctx.phone)
    if flow is None:
        return Transition(
            stage=ConversationStage.IDLE,
            slots=ConversationSlots(),
            reply=render_template(TemplateKey.GREETING_HELP),
        )
    slots.flow = flow
    if flow == ConversationFlow.RESCHEDULE:
        if intent and intent.local_date:
            slots.local_date = intent.local_date
        if intent and intent.local_time:
            slots.local_time = intent.local_time

        if slots.local_date is not None and slots.local_time is not None:
            if slots.old_date_time is None or slots.service_price_cents is None:
                origin = await load_reschedule_origin(ctx, slots)
                if origin is None:
                    slots = ConversationSlots()
                    return Transition(
                        stage=ConversationStage.IDLE,
                        slots=slots,
                        reply=render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                    )
            try:
                details = ConfirmationDetails.from_slots(slots, ctx.business_tz)
                return enter_confirmation(
                    flow=ConversationFlow.RESCHEDULE,
                    slots=slots,
                    details=details,
                )
            except MissingConfirmationDataError as exc:
                return handle_missing_confirmation_data(ctx.phone, slots, exc)
        return Transition(
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots,
            reply=render_template(TemplateKey.ASK_RESCHEDULE_DATETIME),
        )

    # Flow == ConversationFlow.BOOK: merge slots
    active_services = await ctx.reads.list_active_services()
    active_barbers = await ctx.reads.list_active_barbers()

    # Service resolution
    svc_query = intent.service_name if (intent and intent.service_name) else None
    if svc_query is None and slots.service_id is None:
        # Check if user sent service name directly as free text
        res_direct = ctx.entity_resolver.resolve_service(text, active_services)
        if res_direct.matched and res_direct.entity:
            svc_query = text

    if svc_query:
        res_s = ctx.entity_resolver.resolve_service(svc_query, active_services)
        if res_s.matched and res_s.entity:
            slots.service_id = res_s.entity.id
            slots.service_name = res_s.entity.name
            slots.service_price_cents = res_s.entity.price_cents
        else:
            svc_list = ", ".join(
                f"{s.name} ({format_currency_brl(s.price_cents)})" for s in active_services
            )
            return Transition(
                stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
                slots=slots,
                reply=render_template(TemplateKey.SERVICE_NOT_FOUND, services_list=svc_list),
            )

    # Barber resolution
    barber_query = intent.barber_name if (intent and intent.barber_name) else None
    if barber_query:
        res_b = ctx.entity_resolver.resolve_barber(barber_query, active_barbers)
        if res_b.matched:
            if res_b.entity is None:
                slots.barber_id = ANY_BARBER
                slots.barber_name = None
            else:
                slots.barber_id = res_b.entity.id
                slots.barber_name = res_b.entity.name
        else:
            barber_list = ", ".join(b.name for b in active_barbers)
            return Transition(
                stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
                slots=slots,
                reply=render_template(
                    TemplateKey.BARBER_NOT_FOUND,
                    barbers_list=barber_list,
                ),
            )

    # Date & Time extraction
    if intent and intent.local_date:
        slots.local_date = intent.local_date
    if intent and intent.local_time:
        slots.local_time = intent.local_time

    # Customer name extraction: ONLY when awaiting customer_name
    if slots.awaiting == "customer_name":
        raw_candidate = intent.customer_name if (intent and intent.customer_name) else text.strip()
        if is_valid_customer_name(raw_candidate):
            slots.customer_name = raw_candidate
            slots.awaiting = None

    # Check missing slots in order
    if slots.service_id is None:
        slots.awaiting = "service"
        svc_list = ", ".join(
            f"{s.name} ({format_currency_brl(s.price_cents)})" for s in active_services
        )
        return Transition(
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots,
            reply=render_template(TemplateKey.ASK_SERVICE, services_list=svc_list),
        )

    if slots.local_date is None or slots.local_time is None:
        slots.awaiting = "date_time"
        return Transition(
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots,
            reply=render_template(
                TemplateKey.ASK_DATE_TIME,
                service_name=slots.service_name or "serviço",
            ),
        )

    if slots.customer_name is None:
        slots.awaiting = "customer_name"
        return Transition(
            stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
            slots=slots,
            reply=render_template(TemplateKey.ASK_CUSTOMER_NAME),
        )

    # All slots collected! Check availability and advance to AWAITING_CONFIRMATION
    return await validate_booking_slots_and_confirm(ctx, slots)
