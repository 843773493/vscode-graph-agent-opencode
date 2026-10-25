from __future__ import annotations

from langchain_core.messages import AIMessage

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)


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
