"""Confirmation helpers and validation logic for state transitions."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import assert_never
from zoneinfo import ZoneInfo

from booking_agent.database.models import Appointment
from booking_agent.domain.conversation_state import ANY_BARBER, ConversationSlots
from booking_agent.domain.enums import (
    AppointmentStatus,
    ConversationFlow,
    ConversationStage,
)
from booking_agent.domain.errors import MissingConfirmationDataError
from booking_agent.services.booking_service import localize_datetime
from booking_agent.services.templates import (
    TemplateKey,
    format_currency_brl,
    format_short_date_pt_br,
    format_time_pt_br,
    render_template,
)
from booking_agent.services.transitions.context import TransitionContext
from booking_agent.services.transitions.transition import Transition

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
        slots: ConversationSlots,
        business_tz: ZoneInfo,
    ) -> ConfirmationDetails:
        """Build ConfirmationDetails from conversation slots, validating mandatory fields."""
        missing: list[str] = []

        service_val = slots.service_name
        if not service_val:
            missing.append("service")

        formatted_date: str | None = None
        if slots.local_date is None:
            missing.append("date")
        else:
            formatted_date = format_short_date_pt_br(slots.local_date)

        formatted_time: str | None = None
        if slots.local_time is None:
            missing.append("time")
        else:
            formatted_time = format_time_pt_br(slots.local_time)

        name_val = slots.customer_name
        if not name_val:
            missing.append("name")

        formatted_price: str | None = None
        if slots.service_price_cents is None:
            missing.append("price")
        else:
            formatted_price = format_currency_brl(slots.service_price_cents)

        if (
            missing
            or service_val is None
            or formatted_date is None
            or formatted_time is None
            or name_val is None
            or formatted_price is None
        ):
            raise MissingConfirmationDataError(tuple(missing))

        barber_val = slots.barber_name or "qualquer barbeiro disponível"
        old_dt_val = slots.old_date_time

        return cls(
            service=service_val,
            date=formatted_date,
            time=formatted_time,
            name=name_val,
            price=formatted_price,
            old_date_time=old_dt_val,
            barber=barber_val,
        )


def extract_pending_flow(
    stage: str,
    slots: ConversationSlots,
    phone: str,
) -> ConversationFlow | None:
    """Extract and validate the pending flow from state slots.

    If missing or unrecognized, logs an error event without personal data
    and returns None to trigger a fail-safe reset to IDLE.

    Args:
        stage: Current conversation stage.
        slots: State slots instance.
        phone: Customer phone number (masked in logs).

    Returns:
        Validated ConversationFlow member, or None if missing or invalid.
    """
    masked_phone = mask_phone(phone)
    if slots.flow is None:
        logger.error(
            "Missing pending flow in state slots for conversation",
            extra={
                "event": "missing_pending_flow",
                "phone_masked": masked_phone,
                "stage": stage,
            },
        )
        return None

    return slots.flow


def enter_confirmation(
    flow: ConversationFlow,
    slots: ConversationSlots,
    details: ConfirmationDetails,
) -> Transition:
    """Single entry point for transitioning the conversation into AWAITING_CONFIRMATION.

    Generates a new action_id, strictly records slots.flow, and chooses the
    exact confirmation template corresponding to the flow using exhaustive matching.

    Args:
        flow: Active ConversationFlow member.
        slots: Slot instance to persist with the flow and action_id.
        details: Mandatory typed details for template rendering.

    Returns:
        Transition describing the next stage, slots, and reply.

    Raises:
        MissingConfirmationDataError: If old_date_time is missing for RESCHEDULE flow.
    """
    if flow == ConversationFlow.RESCHEDULE and not details.old_date_time:
        raise MissingConfirmationDataError(("old_date_time",))

    slots.action_id = uuid.uuid4()
    slots.flow = flow
    slots.awaiting = None

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

    return Transition(
        stage=ConversationStage.AWAITING_CONFIRMATION,
        slots=slots,
        reply=prompt,
    )


def handle_missing_confirmation_data(
    phone: str,
    slots: ConversationSlots,
    error: MissingConfirmationDataError,
) -> Transition:
    """Handle missing confirmation details: log event without PII and fail safe to IDLE."""
    logger.error(
        "missing_confirmation_data",
        extra={
            "event": "missing_confirmation_data",
            "missing_fields": error.missing_fields,
            "phone_masked": mask_phone(phone),
        },
    )
    slots = ConversationSlots()
    return Transition(
        stage=ConversationStage.IDLE,
        slots=slots,
        reply=render_template(TemplateKey.CONFIRMATION_DATA_UNAVAILABLE),
    )


async def load_reschedule_origin(
    ctx: TransitionContext,
    slots: ConversationSlots,
) -> Appointment | None:
    """Validate and load the confirmed origin appointment for rescheduling.

    Ensures appointment_id is present and a valid UUID, the appointment exists,
    is confirmed, and belongs to the customer of this phone number.
    Populates service_id, barber_id, and old_date_time in slots.

    Args:
        ctx: Transition context with reads port, business timezone and phone.
        slots: State slots instance.

    Returns:
        The confirmed Appointment if valid, None on any failure.
    """
    masked_phone = mask_phone(ctx.phone)

    def _reject(reason: str) -> None:
        logger.error(
            "invalid_reschedule_origin",
            extra={
                "event": "invalid_reschedule_origin",
                "reason": reason,
                "phone_masked": masked_phone,
            },
        )

    raw_id = slots.appointment_id
    if not raw_id:
        _reject("missing_id")
        return None

    try:
        aid = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
    except (ValueError, AttributeError):
        _reject("invalid_id")
        return None

    appt = await ctx.reads.get_appointment(aid)
    if appt is None:
        _reject("not_found")
        return None

    if appt.status != AppointmentStatus.CONFIRMED.value:
        _reject("not_confirmed")
        return None

    customer = await ctx.reads.get_customer_by_phone(ctx.phone)
    if customer is None or appt.customer_id != customer.id:
        _reject("not_owned")
        return None

    if slots.service_id is None:
        slots.service_id = appt.service_id
    if slots.barber_id is None:
        slots.barber_id = appt.barber_id
    if slots.service_name is None or slots.service_price_cents is None:
        svc = await ctx.reads.get_service(appt.service_id)
        if svc:
            if slots.service_name is None:
                slots.service_name = svc.name
            if slots.service_price_cents is None:
                slots.service_price_cents = svc.price_cents
    if slots.barber_name is None and slots.barber_id != ANY_BARBER:
        brb = await ctx.reads.get_barber(appt.barber_id)
        if brb:
            slots.barber_name = brb.name
    if slots.customer_name is None and customer:
        slots.customer_name = customer.name
    if slots.old_date_time is None:
        start_local = appt.starts_at.astimezone(ctx.business_tz)
        slots.old_date_time = (
            f"{format_short_date_pt_br(start_local.date())} às "
            f"{format_time_pt_br(start_local.time())}"
        )

    return appt


async def validate_booking_slots_and_confirm(
    ctx: TransitionContext,
    slots: ConversationSlots,
    flow: ConversationFlow = ConversationFlow.BOOK,
) -> Transition:
    """Validate availability for collected slots and produce confirmation prompt."""
    slots.flow = flow
    if slots.service_id is None or slots.local_date is None or slots.local_time is None:
        return handle_missing_confirmation_data(
            ctx.phone,
            slots,
            MissingConfirmationDataError(("service_id", "local_date", "local_time")),
        )
    service_id = slots.service_id
    target_date = slots.local_date
    target_time = slots.local_time
    barber_id = slots.barber_filter()

    avail = await ctx.reads.list_availability(
        service_id=service_id,
        target_date=target_date,
        barber_id=barber_id,
    )
    requested_dt = localize_datetime(target_date, target_time, ctx.business_tz)

    # Check if requested slot is available
    is_available = any(s == requested_dt for s in avail.slots)

    if is_available:
        try:
            details = ConfirmationDetails.from_slots(slots, ctx.business_tz)
            return enter_confirmation(
                flow=flow,
                slots=slots,
                details=details,
            )
        except MissingConfirmationDataError as exc:
            return handle_missing_confirmation_data(ctx.phone, slots, exc)

    # Slot occupied: propose alternatives
    if avail.slots:
        slots.awaiting = None
        slots.alternative_slots = list(avail.slots[:3])
        slots.flow = flow
        formatted_alts = ", ".join(
            format_time_pt_br(dt.astimezone(ctx.business_tz).time()) for dt in avail.slots[:3]
        )
        resp = render_template(
            TemplateKey.OFFER_ALTERNATIVES,
            requested_time=format_time_pt_br(target_time),
            alternatives_list=formatted_alts,
        )
        return Transition(
            stage=ConversationStage.OFFERING_ALTERNATIVES,
            slots=slots,
            reply=resp,
        )

    # No open slots on that day
    return Transition(
        stage=ConversationStage.COLLECTING_BOOKING_DETAILS,
        slots=slots,
        reply=render_template(TemplateKey.NO_SLOTS_AVAILABLE),
    )
