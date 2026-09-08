from __future__ import annotations

import pytest

from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)


def _accumulator() -> CanonicalBlockAccumulator:
    return CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-stream-contract",
        producer_id="model-call-stream-contract",
    )


def test_partial_text_fragments_remain_partial_until_saver_commits_them() -> None:
    accumulator = _accumulator()
    accumulator.accept(
        {
            "block_id": "text-1",
            "carrier_type": "text",
            "block_index": 0,
            "text": "前半",
        }
    )
    accumulator.accept(
        {
            "block_id": "text-1",
            "carrier_type": "text",
            "block_index": 0,
            "text": "后半",
        }
    )

    items = accumulator.finalize(
        first_item_sequence=1,
        status=CanonicalItemStatus.PARTIAL,
    )

    assert len(items) == 1
    assert items[0].semantic_kind == SemanticKind.ASSISTANT_OUTPUT
    assert items[0].status == CanonicalItemStatus.PARTIAL
    assert items[0].payload == "前半后半"


def test_repeated_block_identity_cannot_change_coordinate_or_carrier() -> None:
    accumulator = _accumulator()
    accumulator.accept(
        {
            "block_id": "block-1",
            "carrier_type": "text",
            "block_index": 0,
            "text": "正文",
        }
    )
    with pytest.raises(ValueError, match="identity"):
        accumulator.accept(
            {
                "block_id": "block-1",
                "carrier_type": "reasoning",
                "block_index": 0,
                "text": "不应拼接",
            }
        )


@pytest.mark.parametrize(
    "block",
    [
        {"carrier_type": "text", "block_index": 0},
        {"block_id": "block-1", "carrier_type": 1, "block_index": 0},
        {"block_id": "block-1", "carrier_type": "text", "block_index": True},
    ],
)
def test_accumulator_rejects_missing_or_coerced_block_identity(
    block: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _accumulator().accept(block)


def test_tool_call_name_may_arrive_in_a_later_delta_but_cannot_be_fabricated() -> None:
    accumulator = _accumulator()
    accumulator.accept(
        {
            "block_id": "call-1",
            "carrier_type": "tool_call",
            "block_index": 0,
            "args": {"path": "README.md"},
        }
    )
    with pytest.raises(ValueError, match="tool_call.name"):
        accumulator.finalize(first_item_sequence=1)

    accumulator = _accumulator()
    accumulator.accept(
        {
            "block_id": "call-1",
            "carrier_type": "tool_call",
            "block_index": 0,
            "args": {"path": "README.md"},
        }
    )
    accumulator.accept(
        {
            "block_id": "call-1",
            "carrier_type": "tool_call",
            "block_index": 0,
            "name": "read_file",
        }
    )
    item = accumulator.finalize(first_item_sequence=1)[0]
    assert item.payload["name"] == "read_file"
