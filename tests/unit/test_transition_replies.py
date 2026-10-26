"""Unit tests for reply classification, tokens and confirmation resolution."""

import pytest

from booking_agent.domain.enums import (
    ConfirmationCandidate,
    ConversationFlow,
)
from booking_agent.services.transitions.replies import (
    AFFIRMATIVE_TOKENS,
    CANCEL_FLOW_TOKENS,
    NEGATIVE_TOKENS,
    classify_fast_path_candidate,
    resolve_confirmation,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate", "flow", "expected"),
    [
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.BOOK, True),
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.RESCHEDULE, True),
        (ConfirmationCandidate.AFFIRMATIVE, ConversationFlow.CANCEL, True),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.BOOK, False),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.RESCHEDULE, False),
        (ConfirmationCandidate.NEGATIVE, ConversationFlow.CANCEL, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.BOOK, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.RESCHEDULE, False),
        (ConfirmationCandidate.CANCEL_ACTION, ConversationFlow.CANCEL, True),
    ],
)
def test_resolve_confirmation_truth_table(
    candidate: ConfirmationCandidate,
    flow: ConversationFlow,
    expected: bool,
) -> None:
    """Parametrized verification of all 9 combinations in the confirmation truth table."""
    assert resolve_confirmation(candidate, flow) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "ambiguous_phrase",
    [
        "não pode cancelar",
        "nao pode cancelar",
        "não cancela",
        "cancela não",
        "não, pode cancelar",
        "pode cancelar sim",
    ],
)
def test_ambiguous_cancellation_phrases_do_not_produce_fast_path_candidate(
    ambiguous_phrase: str,
) -> None:
    """Phrases containing mixed or negated intent must not be classified
    as fast-path candidates.
    """
    assert classify_fast_path_candidate(ambiguous_phrase) is None


@pytest.mark.unit
@pytest.mark.parametrize("token", sorted(AFFIRMATIVE_TOKENS))
def test_affirmative_tokens_classify_correctly(token: str) -> None:
    """All tokens in AFFIRMATIVE_TOKENS classify as ConfirmationCandidate.AFFIRMATIVE."""
    assert classify_fast_path_candidate(token) == ConfirmationCandidate.AFFIRMATIVE


@pytest.mark.unit
@pytest.mark.parametrize("token", sorted(NEGATIVE_TOKENS))
def test_negative_tokens_classify_correctly(token: str) -> None:
    """All tokens in NEGATIVE_TOKENS classify as ConfirmationCandidate.NEGATIVE."""
    assert classify_fast_path_candidate(token) == ConfirmationCandidate.NEGATIVE


@pytest.mark.unit
@pytest.mark.parametrize("token", sorted(CANCEL_FLOW_TOKENS))
def test_cancel_flow_tokens_classify_correctly(token: str) -> None:
    """All tokens in CANCEL_FLOW_TOKENS classify as ConfirmationCandidate.CANCEL_ACTION."""
    assert classify_fast_path_candidate(token) == ConfirmationCandidate.CANCEL_ACTION
