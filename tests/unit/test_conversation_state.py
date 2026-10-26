"""Unit tests for ConversationSlots domain model, round-trip serialization and privacy."""

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from booking_agent.domain.conversation_state import (
    ANY_BARBER,
    ConversationSlots,
    slots_from_json,
    slots_to_json,
)
from booking_agent.domain.enums import ConversationStage
from booking_agent.domain.errors import InvalidConversationStateError


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stage", "slots_dict"),
    [
        (
            ConversationStage.IDLE,
            {},
        ),
        (
            ConversationStage.COLLECTING_BOOKING_DETAILS,
            {
                "flow": "book",
                "service_id": "11111111-1111-1111-1111-111111111111",
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "barber_id": ANY_BARBER,
                "local_date": "2026-11-20",
                "local_time": "14:00:00",
                "awaiting": "customer_name",
            },
        ),
        (
            ConversationStage.OFFERING_ALTERNATIVES,
            {
                "flow": "book",
                "service_id": "11111111-1111-1111-1111-111111111111",
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "barber_id": "22222222-2222-2222-2222-222222222222",
                "barber_name": "Carlos",
                "local_date": "2026-11-20",
                "local_time": "14:00:00",
                "alternative_slots": [
                    "2026-11-20T15:00:00-03:00",
                    "2026-11-20T16:00:00-03:00",
                ],
            },
        ),
        (
            ConversationStage.AWAITING_CONFIRMATION,
            {
                "flow": "reschedule",
                "service_id": "11111111-1111-1111-1111-111111111111",
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "barber_id": ANY_BARBER,
                "local_date": "2026-11-21",
                "local_time": "15:30:00",
                "customer_name": "Renan Maciel",
                "action_id": "33333333-3333-3333-3333-333333333333",
                "appointment_id": "44444444-4444-4444-4444-444444444444",
                "old_date_time": "20/11 às 14:00",
            },
        ),
        (
            ConversationStage.EXECUTING,
            {
                "flow": "book",
                "service_id": "11111111-1111-1111-1111-111111111111",
                "service_name": "Corte de Cabelo",
                "service_price_cents": 4500,
                "barber_id": "22222222-2222-2222-2222-222222222222",
                "barber_name": "Carlos",
                "local_date": "2026-11-20",
                "local_time": "14:00:00",
                "customer_name": "Renan Maciel",
                "action_id": "33333333-3333-3333-3333-333333333333",
                "executing_message_id": "msg_exec_123",
            },
        ),
        (
            ConversationStage.CHOOSING_APPOINTMENT,
            {
                "flow": "cancel",
                "candidate_appointment_ids": [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
            },
        ),
        (
            ConversationStage.COMPLETED,
            {},
        ),
    ],
)
def test_conversation_slots_round_trip_for_all_stages(
    stage: ConversationStage,
    slots_dict: dict[str, Any],
) -> None:
    """Slots for every conversation stage serialize to JSON and back identically."""
    slots = slots_from_json(slots_dict)
    round_tripped = slots_to_json(slots)
    assert round_tripped == slots_dict


@pytest.mark.unit
def test_conversation_slots_round_trip_with_any_barber() -> None:
    """Explicit 'any' value for barber_id survives round-trip identically."""
    data = {"barber_id": ANY_BARBER}
    assert slots_to_json(slots_from_json(data)) == data


@pytest.mark.unit
def test_conversation_slots_round_trip_omits_null_keys() -> None:
    """Keys with null values are omitted from slots_to_json serialization."""
    data = {"flow": "book", "barber_name": None}
    slots = slots_from_json(data)
    assert slots_to_json(slots) == {"flow": "book"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "invalid_data",
    [
        {"unknown_key": "val"},
        {"service_id": "x"},
        {"local_date": "amanha"},
        {"local_date": "2026-13-45"},
        {"local_time": "25:99"},
        {"local_time": "15h"},
        {"flow": "x"},
        {"awaiting": "foo"},
        {"service_price_cents": -1},
        {"executing_message_id": ""},
        {"barber_id": "qualquer"},
        {
            "service": "Corte de Cabelo",
            "date": "2026-10-26",
            "time": "15:00:00",
            "name": "Renan Maciel",
            "price": "R$ 45,00",
            "barber": "Bruno",
        },
    ],
)
def test_conversation_slots_rejection(invalid_data: dict[str, Any]) -> None:
    """Invalid slot keys and values are rejected with a ValidationError."""
    with pytest.raises(ValidationError):
        slots_from_json(invalid_data)


@pytest.mark.unit
def test_conversation_slots_privacy_proof_masks_input_values() -> None:
    """Converted InvalidConversationStateError contains field names only, never sensitive values."""
    sensitive_marker = "SENSITIVE-VALUE-123"
    invalid_dict = {"service_id": sensitive_marker}

    with pytest.raises(ValidationError) as exc_info:
        slots_from_json(invalid_dict)

    # Convert to domain error exactly as done at the repository boundary
    domain_error = InvalidConversationStateError.from_validation_error(exc_info.value)

    assert sensitive_marker not in str(domain_error)
    assert domain_error.invalid_fields == ("service_id",)
    assert str(domain_error) == "Invalid conversation state fields: service_id"


@pytest.mark.unit
def test_conversation_slots_barber_filter() -> None:
    """barber_filter returns None for unset or 'any', and UUID for specific barber."""
    assert ConversationSlots(barber_id=None).barber_filter() is None
    assert ConversationSlots(barber_id=ANY_BARBER).barber_filter() is None

    barber_uuid = UUID("22222222-2222-2222-2222-222222222222")
    assert ConversationSlots(barber_id=barber_uuid).barber_filter() == barber_uuid
