"""Transition handler for the CHOOSING_APPOINTMENT conversation stage."""

from booking_agent.domain.booking_intent import BookingIntent
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.entity_resolver import normalize_text
from booking_agent.services.templates import (
    TemplateKey,
    format_short_date_pt_br,
    format_time_pt_br,
    render_flow_cancelled,
    render_template,
)
from booking_agent.services.transitions.confirmation import (
    ConfirmationDetails,
    enter_confirmation,
    extract_pending_flow,
    handle_missing_confirmation_data,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.replies import NEGATIVE_TOKENS
from booking_agent.services.transitions.selection import extract_selection_index
from booking_agent.services.transitions.transition import Transition


async def handle_choosing_appointment(
    ctx: TransitionContext,
    slots: ConversationSlots,
    text: str,
    intent: BookingIntent | None,
) -> Transition:
    """Handle state transition when customer is choosing among multiple appointments.

    Args:
        ctx: Transition context with reads port, clock, resolver, timezone and phone.
        slots: Current conversation state slots.
        text: Raw reply text from customer.
        intent: Structured booking intent parsed from customer message.

    Returns:
        Resulting Transition with updated stage, slots and response text.
    """
    flow = extract_pending_flow(ConversationStage.CHOOSING_APPOINTMENT.value, slots, ctx.phone)
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

    candidates = slots.candidate_appointment_ids or []
    selected_idx = extract_selection_index(text, len(candidates))
    if selected_idx is not None:
        appointment_id = candidates[selected_idx]
        slots.appointment_id = appointment_id
        slots.candidate_appointment_ids = None
        appt = await ctx.reads.get_appointment(appointment_id)
        if appt is None or appt.status != AppointmentStatus.CONFIRMED.value:
            return Transition(
                stage=ConversationStage.IDLE,
                slots=ConversationSlots(),
                reply=render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
            )

        service = await ctx.reads.get_service(appt.service_id)
        barber = await ctx.reads.get_barber(appt.barber_id)
        cust = await ctx.reads.get_customer(appt.customer_id)
        slots.service_id = appt.service_id
        slots.barber_id = appt.barber_id
        if service:
            slots.service_name = service.name
            slots.service_price_cents = service.price_cents
        if barber:
            slots.barber_name = barber.name
        if cust:
            slots.customer_name = cust.name
        local_start = appt.starts_at.astimezone(ctx.business_tz)
        slots.local_date = local_start.date()
        slots.local_time = local_start.time()
        old_dt_str = (
            f"{format_short_date_pt_br(local_start.date())} às "
            f"{format_time_pt_br(local_start.time())}"
        )
        slots.old_date_time = old_dt_str

        if flow == ConversationFlow.CANCEL:
            try:
                details = ConfirmationDetails.from_slots(slots, ctx.business_tz)
                return enter_confirmation(
                    flow=ConversationFlow.CANCEL,
                    slots=slots,
                    details=details,
                )
            except MissingConfirmationDataError as exc:
                return handle_missing_confirmation_data(ctx.phone, slots, exc)
        else:  # reschedule
            if intent and intent.local_date and intent.local_time:
                slots.local_date = intent.local_date
                slots.local_time = intent.local_time
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

    # Re-prompt appointment list
    candidate_appts = [await ctx.reads.get_appointment(aid) for aid in candidates]
    appt_lines: list[str] = []
    for i, a in enumerate(candidate_appts):
        if a:
            svc = await ctx.reads.get_service(a.service_id)
            brb = await ctx.reads.get_barber(a.barber_id)
            s_name = svc.name if svc else "Serviço"
            b_name = brb.name if brb else "Profissional"
            dt_local = a.starts_at.astimezone(ctx.business_tz)
            appt_lines.append(
                f"{i + 1}. {s_name} em {format_short_date_pt_br(dt_local.date())} "
                f"às {format_time_pt_br(dt_local.time())} com {b_name}"
            )
    return Transition(
        stage=ConversationStage.CHOOSING_APPOINTMENT,
        slots=slots,
        reply=render_template(
            TemplateKey.CHOOSE_APPOINTMENT,
            appointments_list="\n".join(appt_lines),
        ),
    )
