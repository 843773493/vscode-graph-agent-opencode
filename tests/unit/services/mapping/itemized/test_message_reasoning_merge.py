from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage

from app.domain.itemized.hashing import content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionGroup,
    ProjectionPartRef,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)

MESSAGE_TIME = datetime(2026, 7, 14, tzinfo=UTC)


def _item(
    *,
    sequence: int,
    item_id: str,
    model_call_id: str,
    semantic_kind: str,
    payload_kind: str,
    payload: object,
) -> CanonicalItemRecord:
    identity = (
        ItemProjectionIdentity(
            model_call_id=model_call_id,
            block_id=f"{model_call_id}:block:reasoning",
            block_index=0,
        )
        if semantic_kind == "reasoning"
        else ItemProjectionIdentity(model_call_id=model_call_id)
    )
    return CanonicalItemRecord.create(
        item_sequence=sequence,
        item_id=item_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status="completed",
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "test-model",
            "invocation_id": model_call_id,
        },
        payload=payload,
        created_at="2026-10-24T00:00:00+00:00",
        projection_identity=identity,
        turn_id="turn-1",
        turn_scope="turn_member",
    )


def test_unscoped_tool_calls_match_ids_from_any_model_call() -> None:
    canonical_items = [
        _item(
            sequence=1,
            item_id="reasoning-a",
            model_call_id="model-a",
            semantic_kind="reasoning",
            payload_kind="text",
            payload="思考内容",
        ),
        _item(
            sequence=2,
            item_id="tool-call-a",
            model_call_id="model-a",
            semantic_kind="tool_call",
            payload_kind="tool_call",
            payload={"tool_calls": [{"id": "call-a", "name": "read", "args": {}}]},
        ),
        _item(
            sequence=3,
            item_id="tool-call-b",
            model_call_id="model-b",
            semantic_kind="tool_call",
            payload_kind="tool_call",
            payload={"tool_calls": [{"id": "call-b", "name": "read", "args": {}}]},
        ),
    ]
    messages = [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": "read", "args": {}}])
        for call_id in ("call-a", "call-b")
    ]

    projected = merge_canonical_reasoning(messages, canonical_items)

    assert [
        message.response_metadata["reasoning_merge_reason"]
        for message in projected
    ] == ["model_call_scope_missing", "model_call_scope_missing"]
    assert all(message.response_metadata["reasoning_merge_status"] == "skipped_ambiguous_order" for message in projected)
    assert all(message.response_metadata == {} for message in messages)

@pytest.fixture
def reasoning_items_factory():
    """输入顺序来自已提交 selection；物理 sequence 故意与它不同。"""

    def make(*indices: int) -> list[CanonicalItemRecord]:
        items = [
            CanonicalItemRecord.create(
                item_sequence=10 - position,
                item_id=f"reasoning-{index}",
                semantic_kind="reasoning",
                payload_kind="text",
                status="completed",
                producer_ref={
                    "producer_kind": "provider",
                    "producer_id": "test-model",
                    "invocation_id": "model-call-1",
                },
                payload=f"思考 {index}",
                created_at=MESSAGE_TIME.isoformat(),
                message_group_id="message-model-call-1",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-1",
                    block_id=f"model-call-1:block:reasoning-{index}",
                    block_index=index,
                ),
                turn_id="turn-1",
                turn_scope="turn_member",
            )
            for position, index in enumerate(indices)
        ]
        items.append(
            CanonicalItemRecord.create(
                item_sequence=11,
                item_id="tool-call-item",
                semantic_kind="tool_call",
                payload_kind="tool_call",
                status="completed",
                producer_ref={
                    "producer_kind": "provider",
                    "producer_id": "test-model",
                    "invocation_id": "model-call-1",
                },
                payload={"tool_calls": [{"id": "call-1", "name": "read", "args": {}}]},
                created_at=MESSAGE_TIME.isoformat(),
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-1"
                ),
                turn_id="turn-1",
                turn_scope="turn_member",
            )
        )
        return items

    return make


def test_agent_state_reasoning_merge_preserves_selection_order(reasoning_items_factory):
    items = reasoning_items_factory(0, 2)
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content="",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], items)
    assert [block["id"] for block in result[0].content] == [
        "model-call-1:block:reasoning-0",
        "model-call-1:block:reasoning-2",
    ]
    assert message.content == ""
    assert [item.item_sequence for item in items] == [10, 9, 11]
    assert result[0].tool_calls == message.tool_calls


def test_agent_state_reasoning_merge_preserves_text_and_protected_carrier(
    reasoning_items_factory,
):
    protected = {
        "type": "reasoning",
        "id": "reasoning-0",
        "index": 0,
        "reasoning": "思考 0",
        "signature": "protected-signature",
        "extras": {"opaque": "不得丢失"},
    }
    text = {"type": "text", "text": "调用前说明", "index": 1, "id": "text-1"}
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[protected, text],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    items = reasoning_items_factory(0, 2)
    original_items = [item.to_dict() for item in items]
    result = merge_canonical_reasoning([message], items)
    assert result[0].content == [
        protected,
        text,
        {
            "type": "reasoning",
            "reasoning": "思考 2",
            "id": "model-call-1:block:reasoning-2",
            "index": 2,
        },
    ]
    assert message.content == [protected, text]
    assert [item.to_dict() for item in items] == original_items
    assert merge_canonical_reasoning(result, items)[0].content == result[0].content


def test_agent_state_reasoning_merge_reports_unidentified_provider_carrier(
    reasoning_items_factory,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_source"] == "canonical_item_stream"
    assert (
        result[0].response_metadata["reasoning_merge_reason"]
        == "content_part_index_missing"
    )


def test_agent_state_reasoning_merge_deduplicates_stream_and_checkpoint_items(
    reasoning_items_factory,
):
    items = reasoning_items_factory(0)
    items.insert(
        1,
        replace(
            items[0],
            item_id="reasoning-0-checkpoint",
            projection_group=ProjectionGroup.from_dict(
                {"content_form": "list", "ordinal": 0, "size": 2}
            ),
        ),
    )
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], items)
    assert result[0].content == message.content


def test_agent_state_reasoning_merge_excludes_final_carrier_parts_from_tool_message(
    reasoning_items_factory,
):
    items = reasoning_items_factory(0)
    post_tool = replace(
        items[0],
        item_id="reasoning-after-tool",
        item_sequence=8,
        payload="工具返回后的 reasoning",
        content_hash=content_hash("text", "工具返回后的 reasoning"),
        projection_identity=ItemProjectionIdentity(
            model_call_id="model-call-2",
            block_id="model-call-2:block:part-after-tool",
            block_index=0,
        ),
    )
    items.insert(1, post_tool)
    items.append(
        CanonicalItemRecord.create(
            item_sequence=12,
            item_id="final-assistant",
            semantic_kind="assistant_output",
            payload_kind="structured_content",
            status="completed",
            producer_ref={
                "producer_kind": "provider",
                "producer_id": "final-assistant",
                "invocation_id": "turn-1",
            },
            payload=[
                {
                    "reasoning_content": "工具返回后的 reasoning",
                    "type": "reasoning_content",
                },
                {"text": "最终答复", "type": "text"},
            ],
            projection_identity=ItemProjectionIdentity(
                model_call_id="model-call-2",
                phase="final_answer",
                content_part_refs=tuple(
                    ProjectionPartRef.from_dict(ref)
                    for ref in [
                        {"id": "part-after-tool", "index": 0},
                        {"id": "part-final-text", "index": 1},
                    ]
                ),
            ),
            turn_id="turn-1",
            turn_scope="turn_member",
            wire_role="assistant",
        )
    )
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )

    result = merge_canonical_reasoning([message], items)

    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_source"] == "canonical_item_stream"


@pytest.mark.parametrize("indices", [(0, 2), (2, 0)])
def test_agent_state_reasoning_merge_keeps_ambiguous_part_order_explicit(
    reasoning_items_factory,
    indices,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content="缺少 content-part 顺序的正文",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(*indices))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_index_missing"
    )
    assert message.content == result[0].content


def test_agent_state_reasoning_merge_keeps_conflicting_part_order_explicit(
    reasoning_items_factory,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[{"type": "text", "text": "调用前说明", "index": 1}],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(2, 0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_order_conflict"
    )
    assert message.content == result[0].content
