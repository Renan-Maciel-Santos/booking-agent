"""Unit tests for selection and alternative extraction helpers."""

from datetime import datetime

import pytest

from booking_agent.services.transitions.selection import (
    extract_alternative_index,
    extract_selection_index,
)


@pytest.mark.unit
def test_extract_selection_index_valid_number() -> None:
    """Matches valid numeric digits and ordinal words within range."""
    assert extract_selection_index("1", max_count=3) == 0
    assert extract_selection_index("2", max_count=3) == 1
    assert extract_selection_index("3", max_count=3) == 2
    assert extract_selection_index("primeiro", max_count=3) == 0
    assert extract_selection_index("a segunda opção", max_count=3) == 1
    assert extract_selection_index("terceira", max_count=3) == 2


@pytest.mark.unit
def test_extract_selection_index_out_of_range() -> None:
    """Returns None for numbers or ordinals outside the valid index range."""
    assert extract_selection_index("0", max_count=3) is None
    assert extract_selection_index("4", max_count=3) is None
    assert extract_selection_index("10", max_count=3) is None
    assert extract_selection_index("segundo", max_count=1) is None


@pytest.mark.unit
def test_extract_selection_index_text_without_number() -> None:
    """Returns None when the text contains neither digits nor supported ordinal words."""
    assert extract_selection_index("quero agendar", max_count=3) is None
    assert extract_selection_index("cancelar", max_count=3) is None
    assert extract_selection_index("", max_count=3) is None


@pytest.mark.unit
def test_extract_alternative_index_by_number() -> None:
    """Matches alternative by ordinal or numeric selection index."""
    alts = [
        datetime(2026, 10, 24, 14, 0),
        datetime(2026, 10, 24, 15, 0),
        datetime(2026, 10, 24, 16, 0),
    ]
    assert extract_alternative_index("1", alts) == 0
    assert extract_alternative_index("segunda", alts) == 1
    assert extract_alternative_index("4", alts) is None


@pytest.mark.unit
def test_extract_alternative_index_by_time_match() -> None:
    """Matches alternative by matching time string expressions like '15h'."""
    alts: list[datetime | str] = [
        datetime(2026, 10, 24, 14, 0),
        "2026-10-24T15:00:00",
        datetime(2026, 10, 24, 16, 30),
    ]
    assert extract_alternative_index("pode ser as 14h", alts) == 0
    assert extract_alternative_index("15h", alts) == 1
    assert extract_alternative_index("quero as 15h por favor", alts) == 1


@pytest.mark.unit
def test_extract_alternative_index_by_time_unmatched() -> None:
    """Returns None when text contains an unlisted time or irrelevant text."""
    alts = [
        datetime(2026, 10, 24, 14, 0),
        datetime(2026, 10, 24, 15, 0),
    ]
    assert extract_alternative_index("quero as 17h", alts) is None
    assert extract_alternative_index("tanto faz", alts) is None
