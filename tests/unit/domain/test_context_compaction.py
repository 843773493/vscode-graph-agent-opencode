"""容量交集只消费显式边界，输出未知与不可适配分别保留。"""

from __future__ import annotations

import pytest

from app.domain.context_compaction import (
    CompactionPolicy,
    ContextBudget,
    InputTokenEstimate,
    ModelCapacity,
)


@pytest.mark.parametrize("input_limit,window,output_tokens,expected", [
    (None, None, None, None),
    (200000, None, None, 200000),
    (None, 128000, None, 128000),
    (200000, 128000, None, 128000),
    (100000, 128000, None, 100000),
    (200000, None, 16000, 200000),
    (None, None, 16000, None),
    (None, 128000, 16000, 112000),
    (200000, 128000, 16000, 112000),
    (100000, 128000, 16000, 100000),
    (200000, 128000, 128000, 0),
    (200000, 128000, 130000, -2000),
])
def test_capacity_intersects_input_and_total_window_without_inventing_output_reservation(
    input_limit, window, output_tokens, expected,
):
    capacity = ModelCapacity(
        max_input_tokens=input_limit, context_window=window,
        output_reservation_tokens=output_tokens,
    )
    assert capacity.input_budget_tokens == expected
    assert capacity.capacity_known is (input_limit is not None or window is not None)
    assert capacity.output_reservation_known is (output_tokens is not None)
    assert capacity.cannot_fit is (expected is not None and expected <= 0)
    assert ModelCapacity.model_validate(capacity.model_dump()) == capacity


def test_capacity_is_frozen():
    capacity = ModelCapacity(context_window=128000)
    with pytest.raises(ValueError, match="frozen"):
        capacity.context_window = 200000


@pytest.mark.parametrize("tokens,automatic,expected", [
    (79, True, "continue"), (80, True, "prepare"), (89, True, "prepare"), (90, True, "compact"),
    (90, False, "continue"), (101, False, "cannot_fit"), (101, True, "compact"),
])
def test_budget_policy_does_not_turn_automatic_threshold_into_hard_capacity(tokens, automatic, expected):
    budget = ContextBudget(capacity=ModelCapacity(context_window=100),
                           estimate=InputTokenEstimate(known_tokens=tokens, source="test"))
    assert budget.decision(CompactionPolicy(), auto_summarize=automatic) == expected
    assert budget.capacity.output_reservation_known is False


def test_unknown_media_does_not_gain_fraction_from_known_capacity():
    budget = ContextBudget(capacity=ModelCapacity(context_window=100), estimate=InputTokenEstimate(
        known_tokens=80, unknown_parts=("input_audio",), source="test",
    ))
    assert budget.input_fraction is None
    assert budget.decision(CompactionPolicy(), auto_summarize=True) == "unknown"
    assert budget.model_copy(update={"capacity": ModelCapacity()}).input_fraction is None


def test_negative_output_room_cannot_be_repaired_by_summarization():
    budget = ContextBudget(capacity=ModelCapacity(context_window=100, output_reservation_tokens=101),
                           estimate=InputTokenEstimate(known_tokens=0, source="test"))
    assert budget.decision(CompactionPolicy(), auto_summarize=True) == "cannot_fit"
