"""Reply classification and confirmation resolution helpers."""

from typing import assert_never

from booking_agent.domain.enums import (
    ConfirmationCandidate,
    ConversationFlow,
)
from booking_agent.services.entity_resolver import normalize_text

AFFIRMATIVE_TOKENS: frozenset[str] = frozenset(
    {
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
)

NEGATIVE_TOKENS: frozenset[str] = frozenset(
    {
        "nao",
        "não",
        "n",
        "deixa",
        "esquece",
    }
)

CANCEL_FLOW_TOKENS: frozenset[str] = frozenset(
    {
        "cancela",
        "cancelar",
        "pode cancelar",
        "sim pode cancelar",
    }
)


def classify_fast_path_candidate(text: str) -> ConfirmationCandidate | None:
    """Classify user text into a confirmation candidate category, or None if non-exact.

    Args:
        text: Raw reply text from customer.

    Returns:
        Categorized ConfirmationCandidate or None if input requires full parsing.
    """
    normalized_msg = normalize_text(text)
    if normalized_msg in AFFIRMATIVE_TOKENS:
        return ConfirmationCandidate.AFFIRMATIVE
    if normalized_msg in NEGATIVE_TOKENS:
        return ConfirmationCandidate.NEGATIVE
    if normalized_msg in CANCEL_FLOW_TOKENS:
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
