"""Selection extraction helpers for choosing appointments and alternative slots."""

import re
from collections.abc import Sequence
from datetime import datetime

from booking_agent.services.entity_resolver import normalize_text


def extract_selection_index(text: str, max_count: int) -> int | None:
    """Extract an ordinal or numeric index (0-based) from customer reply.

    Args:
        text: Raw reply text from customer.
        max_count: Maximum number of options presented.

    Returns:
        0-based index if matched, None otherwise.
    """
    cleaned = normalize_text(text)
    match = re.search(r"\b(\d+)\b", cleaned)
    if match:
        idx = int(match.group(1)) - 1
        if 0 <= idx < max_count:
            return idx
    word_map = {
        "primeiro": 0,
        "primeira": 0,
        "1o": 0,
        "1a": 0,
        "um": 0,
        "segundo": 1,
        "segunda": 1,
        "2o": 1,
        "2a": 1,
        "dois": 1,
        "terceiro": 2,
        "terceira": 2,
        "3o": 2,
        "3a": 2,
        "tres": 2,
    }
    for word, idx in word_map.items():
        if word in cleaned and idx < max_count:
            return idx
    return None


def extract_alternative_index(text: str, alternatives: Sequence[datetime | str]) -> int | None:
    """Extract which alternative slot the user selected by ordinal or time match.

    Args:
        text: Raw reply text from customer.
        alternatives: Sequence of alternative slot datetimes or ISO strings.

    Returns:
        0-based index of matched alternative, None otherwise.
    """
    cleaned = normalize_text(text)
    idx = extract_selection_index(text, len(alternatives))
    if idx is not None:
        return idx
    for i, alt in enumerate(alternatives):
        dt = datetime.fromisoformat(alt) if isinstance(alt, str) else alt
        time_str_1 = dt.strftime("%H:%M")
        time_str_2 = f"{dt.hour}h"
        time_str_3 = f"{dt.hour}:{dt.minute:02d}"
        if time_str_1 in cleaned or time_str_2 in cleaned or time_str_3 in cleaned:
            return i
    return None
