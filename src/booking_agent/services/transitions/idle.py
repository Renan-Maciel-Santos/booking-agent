"""Handler for IDLE stage transitions."""

from __future__ import annotations

from booking_agent.domain.booking_intent import BookingIntent, IntentType
from booking_agent.domain.conversation_state import ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.templates import (
    TemplateKey,
    format_short_date_pt_br,
    format_time_pt_br,
    render_template,
)
from booking_agent.services.transitions.collecting import handle_collecting_booking_details
from booking_agent.services.transitions.confirmation import (
    ConfirmationDetails,
    enter_confirmation,
    handle_missing_confirmation_data,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.transition import Transition


async def handle_idle(
    ctx: TransitionContext,
    slots: ConversationSlots,
    text: str,
    intent: BookingIntent | None,
) -> Transition:
    """Handle state transitions when in IDLE."""
    if intent is None:
        return Transition(
            stage=ConversationStage.IDLE,
            slots=ConversationSlots(),
            reply=render_template(TemplateKey.GREETING_HELP),
        )

    if intent.intent == IntentType.CANCEL or intent.intent == IntentType.RESCHEDULE:
        flow = (
            ConversationFlow.CANCEL
            if intent.intent == IntentType.CANCEL
            else ConversationFlow.RESCHEDULE
        )
        slots.flow = flow
        customer = await ctx.reads.get_customer_by_phone(ctx.phone)
        if customer is None:
            return Transition(
                stage=ConversationStage.IDLE,
                slots=ConversationSlots(),
                reply=render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
            )
        appts = await ctx.reads.list_customer_appointments(customer.id)
        now_utc = ctx.clock.now()
        future_appts = [
            a
            for a in appts
            if a.status == AppointmentStatus.CONFIRMED.value and a.starts_at > now_utc
        ]
        if not future_appts:
            return Transition(
                stage=ConversationStage.IDLE,
                slots=ConversationSlots(),
                reply=render_template(TemplateKey.NO_APPOINTMENTS_FOUND),
            )

        if len(future_appts) == 1:
            target_appt = future_appts[0]
            slots.appointment_id = target_appt.id
            service = await ctx.reads.get_service(target_appt.service_id)
            barber = await ctx.reads.get_barber(target_appt.barber_id)
            slots.service_id = target_appt.service_id
            slots.barber_id = target_appt.barber_id
            if service:
                slots.service_name = service.name
                slots.service_price_cents = service.price_cents
            if barber:
                slots.barber_name = barber.name
            slots.customer_name = customer.name
            start_local = target_appt.starts_at.astimezone(ctx.business_tz)
            slots.local_date = start_local.date()
            slots.local_time = start_local.time()
            old_dt_str = (
                f"{format_short_date_pt_br(start_local.date())} às "
                f"{format_time_pt_br(start_local.time())}"
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
                if intent.local_date and intent.local_time:
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

        # Multiple appointments -> CHOOSING_APPOINTMENT
        slots.candidate_appointment_ids = [a.id for a in future_appts]
        appt_lines = []
        for i, a in enumerate(future_appts):
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

    if intent.intent == IntentType.BOOK:
        slots.flow = ConversationFlow.BOOK
        return await handle_collecting_booking_details(ctx, slots, text, intent)

    return Transition(
        stage=ConversationStage.IDLE,
        slots=ConversationSlots(),
        reply=render_template(TemplateKey.GREETING_HELP),
    )
