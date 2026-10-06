from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)


def _accumulator() -> CanonicalBlockAccumulator:
    return CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-stream-contract",
        producer_id="model-call-stream-contract",
    )


@pytest.fixture
def codec() -> LangChainMessageCodec:
    return LangChainMessageCodec()


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


def test_internal_provider_item_keeps_execution_attribution_without_turn_scope() -> None:
    accumulator = CanonicalBlockAccumulator(
        turn_id=None,
        execution_id="execution-internal-stream",
        producer_id="model-call-internal-stream",
        model_call_id="model-call-internal-stream",
    )
    accumulator.accept(
        {
            "block_id": "internal-text-1",
            "carrier_type": "text",
            "block_index": 0,
            "text": "Turn-less output",
        }
    )

    item = accumulator.finalize(first_item_sequence=1)[0]

    assert item.turn_id is None
    assert item.turn_scope is None
    assert item.projection_identity.model_call_id == "model-call-internal-stream"
    assert item.producer_ref["invocation_id"] == "model-call-internal-stream"


def test_message_group_is_scoped_to_model_call_within_one_execution() -> None:
    first = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-1",
        model_call_id="model-call-1",
    )
    second = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-2",
        model_call_id="model-call-2",
    )
    for accumulator, block_id in ((first, "call-1"), (second, "call-2")):
        accumulator.accept(
            {
                "block_id": block_id,
                "carrier_type": "tool_call",
                "block_index": 0,
                "name": "read_file",
                "args": {"path": "README.md"},
            }
        )

    first_item = first.finalize(first_item_sequence=1)[0]
    second_item = second.finalize(first_item_sequence=2)[0]

    assert first_item.message_group_id == "message-model-call-1"
    assert second_item.message_group_id == "message-model-call-2"
    assert first_item.message_group_id != second_item.message_group_id
    assert first_item.item_id == (
        "item-execution-shared-model-model-call-1-block-call-1"
    )
    assert second_item.item_id == (
        "item-execution-shared-model-model-call-2-block-call-2"
    )
    assert first_item.item_id != second_item.item_id


def test_reused_provider_block_id_is_unique_per_model_call() -> None:
    first = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-1",
        model_call_id="model-call-1",
    )
    second = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-2",
        model_call_id="model-call-2",
    )
    for accumulator in (first, second):
        accumulator.accept(
            {
                "block_id": "provider-reused-block",
                "carrier_type": "reasoning_content",
                "block_index": 0,
                "text": "同一个 provider block id",
            }
        )

    first_item = first.finalize(first_item_sequence=1)[0]
    second_item = second.finalize(first_item_sequence=2)[0]

    assert first_item.item_id != second_item.item_id
    assert first_item.projection_identity.model_call_id == "model-call-1"
    assert second_item.projection_identity.model_call_id == "model-call-2"


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


def test_retry_stream_and_final_checkpoint_keep_the_same_current_model_scope(
    codec: LangChainMessageCodec,
) -> None:
    failed = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-failed",
        model_call_id="model-call-failed",
    )
    completed = CanonicalBlockAccumulator(
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        producer_id="model-call-current",
        model_call_id="model-call-current",
    )
    for accumulator, text in ((failed, "失败片段"), (completed, "最终回答")):
        accumulator.accept(
            {
                "block_id": "provider-text-0",
                "carrier_type": "text",
                "block_index": 0,
                "text": text,
            }
        )
    failed_item = failed.finalize(
        first_item_sequence=1, status=CanonicalItemStatus.FAILED
    )[0]
    stream_item = completed.finalize(first_item_sequence=2)[0]
    final = AIMessage(
        id="msg_retry_final",
        content="最终回答",
        response_metadata={"model_call_id": "model-call-current"},
    )
    final_item = codec.items_for_message(
        final,
        item_sequence=3,
        message_id=final.id,
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        timestamp=stream_item.created_at,
    )[0]
    replay = codec.from_dict(codec.project_message((final_item,)))
    replayed_item = codec.items_for_message(
        replay,
        item_sequence=3,
        message_id=final.id,
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        timestamp=stream_item.created_at,
    )[0]

    assert failed_item.message_group_id != stream_item.message_group_id
    assert failed_item.item_id != stream_item.item_id
    assert final_item.projection_identity.model_call_id == (
        stream_item.projection_identity.model_call_id
    ) == "model-call-current"
    assert final_item.payload == stream_item.payload == "最终回答"
    assert replayed_item.projection_manifest() == final_item.projection_manifest()
    assert final_item.producer_ref["invocation_id"] == "execution-shared"


@pytest.mark.parametrize(
    "message_id, response_metadata",
    [
        ("msg_final", {"model_call_id": "model-current"}),
        ("msg_final", {"message_metadata": {"model_call_id": "model-current"}}),
        ("lc_run--model-current", {}),
    ],
)
def test_checkpoint_rejects_caller_model_scope_that_conflicts_with_its_message(
    codec: LangChainMessageCodec,
    message_id: str,
    response_metadata: dict[str, object],
) -> None:
    message = AIMessage(
        id=message_id,
        content="最终回答",
        response_metadata=response_metadata,
    )

    with pytest.raises(ValueError, match="model_call_id 与消息自身身份不一致"):
        codec.items_for_message(
            message,
            item_sequence=1,
            message_id=message_id,
            turn_id="turn-stream-contract",
            execution_id="execution-shared",
            timestamp="2026-10-04T00:00:00+00:00",
            model_call_id="model-previous",
        )


def test_tool_checkpoint_uses_the_preceding_assistant_model_scope(
    codec: LangChainMessageCodec,
) -> None:
    assistant = AIMessage(
        id="lc_run--model-tool-call",
        content="",
        tool_calls=[
            {"id": "call-1", "name": "read_file", "args": {"path": "README.md"}}
        ],
    )
    tool = ToolMessage(
        id="msg_tool_result",
        tool_call_id="call-1",
        name="read_file",
        content="文件正文",
    )
    assert codec.model_call_id(tool) is None
    binding = codec.tool_message_model_call_id(tool, (assistant,))
    item = codec.items_for_message(
        tool,
        item_sequence=1,
        message_id=tool.id,
        turn_id="turn-stream-contract",
        execution_id="execution-shared",
        timestamp="2026-10-04T00:00:00+00:00",
        model_call_id=binding,
    )[0]

    assert binding == item.projection_identity.model_call_id == "model-tool-call"
    assert item.semantic_kind == SemanticKind.TOOL_RESULT
    assert item.payload["tool_call_id"] == "call-1"
