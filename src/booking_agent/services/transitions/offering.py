"""Transition handler for the OFFERING_ALTERNATIVES conversation stage."""

from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import ConversationFlow, ConversationStage
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.entity_resolver import normalize_text
from booking_agent.services.templates import (
    TemplateKey,
    render_flow_cancelled,
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
from booking_agent.services.transitions.replies import NEGATIVE_TOKENS
from booking_agent.services.transitions.selection import extract_alternative_index
from booking_agent.services.transitions.transition import Transition


async def handle_offering_alternatives(
    ctx: TransitionContext,
    slots: ConversationSlots,
    text: str,
    intent: BookingIntent | None,
) -> Transition:
    """Handle state transition when offering alternative time slots to customer.

    Args:
        ctx: Transition context with reads port, clock, resolver, timezone and phone.
        slots: Current conversation state slots.
        text: Raw reply text from customer.
        intent: Structured booking intent parsed from customer message.

    Returns:
        Resulting Transition with updated stage, slots and response text.
    """
    flow = extract_pending_flow(ConversationStage.OFFERING_ALTERNATIVES.value, slots, ctx.phone)
    if flow is None:
        return Transition(
            stage=ConversationStage.IDLE,
            slots=ConversationSlots(),
            reply=render_template(TemplateKey.GREETING_HELP),
        )
    slots.flow = flow

    normalized_msg = normalize_text(text)
    if normalized_msg in NEGATIVE_TOKENS:
        response = render_flow_cancelled(flow)
        return Transition(
            stage=ConversationStage.IDLE,
            slots=ConversationSlots(),
            reply=response,
        )

    alts = slots.alternative_slots or []
    chosen_idx = extract_alternative_index(text, alts)
    if chosen_idx is not None:
        chosen_dt = alts[chosen_idx].astimezone(ctx.business_tz)
        slots.local_date = chosen_dt.date()
        slots.local_time = chosen_dt.time()
        slots.alternative_slots = None
        if flow == ConversationFlow.RESCHEDULE and (
            slots.old_date_time is None or slots.service_price_cents is None
        ):
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
                flow=flow,
                slots=slots,
                details=details,
            )
        except MissingConfirmationDataError as exc:
            return handle_missing_confirmation_data(ctx.phone, slots, exc)

    if intent and (intent.local_date or intent.local_time):
        if intent.local_date:
            slots.local_date = intent.local_date
        if intent.local_time:
            slots.local_time = intent.local_time
        slots.alternative_slots = None
        return await validate_booking_slots_and_confirm(ctx, slots, flow=flow)

    return Transition(
        stage=ConversationStage.OFFERING_ALTERNATIVES,
        slots=slots,
        reply=render_template(TemplateKey.CLARIFICATION_PROMPT),
    )
