import pytest

from app.services.mapping.itemized.history_turns.projection import (
    HistoryProjectionBudget,
    HistoryProjectionLimits,
    bounded_text,
)


@pytest.mark.parametrize("character", ["x", "中", "\n"])
def test_text_within_character_limit_preserves_the_complete_answer(character):
    text = character * (64 * 1024)
    projected, truncated = bounded_text(text, HistoryProjectionBudget())

    assert projected == text
    assert truncated is False


def test_multibyte_text_still_obeys_page_byte_budget():
    budget = HistoryProjectionBudget(HistoryProjectionLimits(bytes=1024))
    text = "中" * 1024
    projected, truncated = bounded_text(text, budget)

    assert projected and text.startswith(projected)
    assert truncated is True
    assert budget.bytes_used <= 1024
