"""Unit tests for domain enums."""

from booking_agent.domain.enums import (
    AppointmentStatus,
    ConfirmationCandidate,
    ConversationFlow,
    MessageStatus,
)


def test_appointment_status_values() -> None:
    """Verify AppointmentStatus enum members and string values."""
    assert AppointmentStatus.CONFIRMED.value == "confirmed"
    assert AppointmentStatus.CANCELLED.value == "cancelled"
    assert AppointmentStatus.COMPLETED.value == "completed"
    assert AppointmentStatus("confirmed") is AppointmentStatus.CONFIRMED
    assert set(AppointmentStatus) == {
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.COMPLETED,
    }


def test_message_status_values() -> None:
    """Verify MessageStatus enum members and string values."""
    assert MessageStatus.PROCESSING.value == "processing"
    assert MessageStatus.DONE.value == "done"
    assert MessageStatus.FAILED.value == "failed"
    assert MessageStatus("processing") is MessageStatus.PROCESSING
    assert set(MessageStatus) == {
        MessageStatus.PROCESSING,
        MessageStatus.DONE,
        MessageStatus.FAILED,
    }


def test_conversation_flow_values() -> None:
    """Verify ConversationFlow enum members and string values."""
    assert ConversationFlow.BOOK.value == "book"
    assert ConversationFlow.RESCHEDULE.value == "reschedule"
    assert ConversationFlow.CANCEL.value == "cancel"
    assert ConversationFlow("book") is ConversationFlow.BOOK
    assert set(ConversationFlow) == {
        ConversationFlow.BOOK,
        ConversationFlow.RESCHEDULE,
        ConversationFlow.CANCEL,
    }


def test_confirmation_candidate_values() -> None:
    """Verify ConfirmationCandidate enum members and string values."""
    assert ConfirmationCandidate.AFFIRMATIVE.value == "affirmative"
    assert ConfirmationCandidate.NEGATIVE.value == "negative"
    assert ConfirmationCandidate.CANCEL_ACTION.value == "cancel_action"
    assert ConfirmationCandidate("affirmative") is ConfirmationCandidate.AFFIRMATIVE
    assert set(ConfirmationCandidate) == {
        ConfirmationCandidate.AFFIRMATIVE,
        ConfirmationCandidate.NEGATIVE,
        ConfirmationCandidate.CANCEL_ACTION,
    }
