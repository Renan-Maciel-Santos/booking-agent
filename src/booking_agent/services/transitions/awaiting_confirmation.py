"""Transition handler for the AWAITING_CONFIRMATION conversation stage."""

from typing import assert_never

from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import (
    ANY_BARBER,
    ConversationSlots,
)
from booking_agent.domain.enums import (
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
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
from booking_agent.services.transitions.replies import (
    classify_fast_path_candidate,
    resolve_confirmation,
)
from booking_agent.services.transitions.transition import Transition


async def handle_awaiting_confirmation(
    ctx: TransitionContext,
    slots: ConversationSlots,
    text: str,
    intent: BookingIntent | None,
) -> Transition:
    """Handle state transition when customer is awaiting confirmation.

    Args:
        ctx: Transition context with reads port, clock, resolver, timezone and phone.
        slots: Current conversation state slots.
        text: Raw reply text from customer.
        intent: Structured booking intent parsed from customer message.

    Returns:
        State transition with next stage, updated slots, and reply text.
    """
    pending_flow = extract_pending_flow(
        ConversationStage.AWAITING_CONFIRMATION.value, slots, ctx.phone
    )
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
                details = ConfirmationDetails.from_slots(slots, ctx.business_tz)
                if pending_flow == ConversationFlow.RESCHEDULE and not details.old_date_time:
                    raise MissingConfirmationDataError(("old_date_time",))
            except MissingConfirmationDataError as exc:
                return handle_missing_confirmation_data(ctx.phone, slots, exc)
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
        active_services = await ctx.reads.list_active_services()
        active_barbers = await ctx.reads.list_active_barbers()

        if intent.service_name:
            res_s = ctx.entity_resolver.resolve_service(intent.service_name, active_services)
            if res_s.matched and res_s.entity:
                slots.service_id = res_s.entity.id
                slots.service_name = res_s.entity.name
                slots.service_price_cents = res_s.entity.price_cents
                has_updates = True

        if intent.barber_name:
            res_b = ctx.entity_resolver.resolve_barber(intent.barber_name, active_barbers)
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
                    return await validate_booking_slots_and_confirm(
                        ctx, slots, flow=ConversationFlow.BOOK
                    )
                case ConversationFlow.RESCHEDULE:
                    origin = await load_reschedule_origin(ctx, slots)
                    if origin is None:
                        slots = ConversationSlots()
                        return Transition(
                            stage=ConversationStage.IDLE,
                            slots=slots,
                            reply=render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                        )
                    return await validate_booking_slots_and_confirm(
                        ctx, slots, flow=ConversationFlow.RESCHEDULE
                    )
                case ConversationFlow.CANCEL:
                    origin = await load_reschedule_origin(ctx, slots)
                    if origin is None:
                        slots = ConversationSlots()
                        return Transition(
                            stage=ConversationStage.IDLE,
                            slots=slots,
                            reply=render_template(TemplateKey.RESCHEDULE_ORIGIN_NOT_FOUND),
                        )
                    return await validate_booking_slots_and_confirm(
                        ctx, slots, flow=ConversationFlow.RESCHEDULE
                    )
                case _ as unreachable:
                    assert_never(unreachable)

    # Re-present confirmation prompt if user input was unclear
    if pending_flow == ConversationFlow.BOOK:
        try:
            details = ConfirmationDetails.from_slots(slots, ctx.business_tz)
            return enter_confirmation(
                flow=pending_flow,
                slots=slots,
                details=details,
            )
        except MissingConfirmationDataError as exc:
            return handle_missing_confirmation_data(ctx.phone, slots, exc)

    if pending_flow in (ConversationFlow.RESCHEDULE, ConversationFlow.CANCEL):
        if slots.appointment_id is not None and (
            slots.old_date_time is None
            or slots.service_price_cents is None
            or slots.customer_name is None
        ):
            await load_reschedule_origin(ctx, slots)
        try:
            ConfirmationDetails.from_slots(slots, ctx.business_tz)
            if pending_flow == ConversationFlow.RESCHEDULE and not slots.old_date_time:
                raise MissingConfirmationDataError(("old_date_time",))
        except MissingConfirmationDataError as exc:
            return handle_missing_confirmation_data(ctx.phone, slots, exc)

    return Transition(
        stage=ConversationStage.AWAITING_CONFIRMATION,
        slots=slots,
        reply=render_template(TemplateKey.CLARIFICATION_PROMPT),
    )
