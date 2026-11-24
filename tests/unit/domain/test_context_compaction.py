"""容量交集只消费显式边界，输出未知与不可适配分别保留。"""

from __future__ import annotations

import pytest

from app.domain.context_compaction import ModelCapacity


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
