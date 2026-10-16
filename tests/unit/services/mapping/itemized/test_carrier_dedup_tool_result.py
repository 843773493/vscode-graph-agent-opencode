"""carrier_dedup 的 tool_result shadow 与真实 terminal 保留合同。

背景（D1-investigation.md §2.4/§6.2）：``superseded_tool_group_item_ids``
把 live tool_result 判为 shadow 时，注释假定 checkpoint 的完整 result carrier
会被保留；若 selection 里根本没有该 carrier（D1 缺陷：request 侧 carrier 被
等价复用吞掉），旧实现会静默产出缺失尾部 ToolMessage 的投影。修复后的合同：

1. 无替代 result carrier 时保留唯一真实 tool_result，不补造结果；
2. 存在带投影身份的替代 carrier → 照常过滤 live shadow，carrier 保留；
3. 无 checkpoint 组（确认 carrier 未进入 view）→ 不触发任何过滤。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.hashing import content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionGroup,
    ReasoningCarrier,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.carrier_dedup import (
    superseded_item_ids,
    superseded_tool_group_item_ids,
)

TURN = "job_parent_turn2"
EXECUTION = "execution-parent-turn2"
MODEL_CALL = "01a0a5cc-a65d-7dd2-acfb-9dad99508516"
TOOL_CALL_ID = "child-thread-task-call"
SCOPED_TOOL_CALL_ID = f"{MODEL_CALL}:tool-call:{TOOL_CALL_ID}"
RESULT_CONTENT = '{"child_session_id": "ses_child", "status": "accepted"}'
LIVE_EXECUTION_ID = "01a0a5cc-a8a5-77f1-aed0-ecb624dcda2e"


def _item(
    sequence: int,
    item_id: str,
    semantic_kind: SemanticKind,
    payload_kind: PayloadKind,
    payload: object,
    *,
    wire_role: str,
    projection_identity: ItemProjectionIdentity,
    projection_group: ProjectionGroup | None = None,
    message_group_id: str | None = None,
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=sequence,
        item_id=item_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status=CanonicalItemStatus.COMPLETED,
        turn_id=TURN,
        turn_scope=TurnScope.TURN_MEMBER,
        wire_role=wire_role,
        message_group_id=message_group_id,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": MODEL_CALL,
            "invocation_id": TURN,
        },
        projection_identity=projection_identity,
        projection_group=projection_group,
        payload=payload,
    )


def _stream_reasoning(sequence: int = 1) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-{EXECUTION}-model-{MODEL_CALL}-block-{MODEL_CALL}:block:part_reasoning",
        SemanticKind.REASONING,
        PayloadKind.TEXT,
        "用户要求委派子代理。",
        wire_role="assistant",
        message_group_id=f"message-{MODEL_CALL}",
        projection_identity=ItemProjectionIdentity(
            model_call_id=MODEL_CALL,
            block_id=f"{MODEL_CALL}:block:part_reasoning",
            block_index=0,
        ),
    )


def _stream_tool_call(sequence: int = 2) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-{EXECUTION}-model-{MODEL_CALL}-block-{SCOPED_TOOL_CALL_ID}",
        SemanticKind.TOOL_CALL,
        PayloadKind.TOOL_CALL,
        {
            "args": {"description": "完成示例任务并输出结果"},
            "name": "task",
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": f"tool-invocation:{MODEL_CALL}:{TOOL_CALL_ID}",
        },
        wire_role="assistant",
        message_group_id=f"message-{MODEL_CALL}",
        projection_identity=ItemProjectionIdentity(
            model_call_id=MODEL_CALL, block_id=SCOPED_TOOL_CALL_ID, block_index=0
        ),
    )


def _live_tool_result(sequence: int = 3) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}",
        SemanticKind.TOOL_RESULT,
        PayloadKind.TOOL_RESULT,
        {
            "content": RESULT_CONTENT,
            "name": "task",
            "result_id": LIVE_EXECUTION_ID,
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": f"tool-invocation:{MODEL_CALL}:{TOOL_CALL_ID}",
            "tool_outcome": "success",
            "execution_confirmed": True,
        },
        wire_role="tool",
        message_group_id=f"message-{LIVE_EXECUTION_ID}",
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL),
    )


def _confirmed_reasoning(sequence: int = 4) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-lc_run--{MODEL_CALL}-content-0",
        SemanticKind.REASONING,
        PayloadKind.TEXT,
        "用户要求委派子代理。",
        wire_role="assistant",
        message_group_id=f"message-lc_run--{MODEL_CALL}",
        projection_identity=ItemProjectionIdentity(
            model_call_id=MODEL_CALL,
            reasoning_carrier=ReasoningCarrier.from_dict(
                {
                    "text_key": "reasoning_content",
                    "type": "reasoning_content",
                }
            ),
        ),
        projection_group=ProjectionGroup.from_dict(
            {"content_form": "list", "ordinal": 0, "size": 2}
        ),
    )


def _confirmed_tool_call(sequence: int = 5) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-lc_run--{MODEL_CALL}",
        SemanticKind.TOOL_CALL,
        PayloadKind.TOOL_CALL,
        {
            "tool_calls": [
                {
                    "args": {"description": "完成示例任务并输出结果"},
                    "id": TOOL_CALL_ID,
                    "name": "task",
                    "type": "tool_call",
                }
            ]
        },
        wire_role="assistant",
        message_group_id=f"message-lc_run--{MODEL_CALL}",
        projection_identity=ItemProjectionIdentity(
            projection_message_id=f"lc_run--{MODEL_CALL}", model_call_id=MODEL_CALL
        ),
        projection_group=ProjectionGroup.from_dict(
            {"content_form": "list", "ordinal": 1, "size": 2}
        ),
    )


def _request_tool_result(sequence: int = 6) -> CanonicalItemRecord:
    return _item(
        sequence,
        "item-1b56a352-af40-4db3-adc2-0a77994e039e",
        SemanticKind.TOOL_RESULT,
        PayloadKind.TOOL_RESULT,
        {
            "content": RESULT_CONTENT,
            "name": "task",
            "result_id": "1b56a352-af40-4db3-adc2-0a77994e039e",
            "tool_call_id": TOOL_CALL_ID,
            "tool_outcome": "success",
            "execution_confirmed": True,
        },
        wire_role="tool",
        message_group_id="message-1b56a352-af40-4db3-adc2-0a77994e039e",
        projection_identity=ItemProjectionIdentity(
            projection_message_id="1b56a352-af40-4db3-adc2-0a77994e039e",
            model_call_id=MODEL_CALL,
        ),
    )


def _with_outcome(item: CanonicalItemRecord, outcome: str) -> CanonicalItemRecord:
    payload = {**item.payload, "tool_outcome": outcome, "execution_confirmed": outcome != "unknown"}
    return replace(item, payload=payload, content_hash=content_hash(item.payload_kind, payload))


@pytest.mark.parametrize("outcome", ["success", "failure", "unknown"])
def test_unique_tool_result_kept_without_replacement(outcome: str):
    """checkpoint call 已提交、结果未入 channel 时保留真实 live terminal。"""
    live = _with_outcome(_live_tool_result(), outcome)
    items = (
        _stream_reasoning(),
        _stream_tool_call(),
        live,
        _confirmed_reasoning(),
        _confirmed_tool_call(),
    )
    assert live.item_id not in superseded_tool_group_item_ids(items)
    assert live.item_id not in superseded_item_ids(items)
    assistant, result = project_canonical_items(items, preserve_order=True)
    assert [call["id"] for call in assistant.tool_calls] == [result.tool_call_id] == [TOOL_CALL_ID]
    assert result.content == RESULT_CONTENT
    assert result.status == ("success" if outcome == "success" else "error")
    assert result.response_metadata["tool_outcome"] == outcome


@pytest.mark.parametrize("outcome", ["success", "failure", "unknown"])
def test_live_result_filtered_only_with_replacement_carrier(outcome: str):
    """存在带投影身份的替代 carrier 时照常过滤 live shadow。"""
    items = (
        _stream_reasoning(),
        _stream_tool_call(),
        _with_outcome(_live_tool_result(), outcome),
        _confirmed_reasoning(),
        _confirmed_tool_call(),
        _with_outcome(_request_tool_result(), outcome),
    )
    superseded = superseded_item_ids(items)
    live = _live_tool_result()
    carrier = _request_tool_result()
    # stream assistant 组与 live result 被 shadow，替代 carrier 保留。
    assert live.item_id in superseded
    assert carrier.item_id not in superseded
    messages = project_canonical_items(
        tuple(sorted(items, key=lambda item: item.item_sequence)),
        preserve_order=True,
    )
    assert any(type(message).__name__ == "ToolMessage" for message in messages)


def test_other_turn_result_cannot_replace_original_terminal():
    """相同 model-call 的错误跨 Turn carrier 不得遮蔽原始真实结果。"""
    live = _with_outcome(_live_tool_result(), "unknown")
    foreign = replace(_request_tool_result(), turn_id="other-turn")
    items = (_stream_tool_call(), _confirmed_tool_call(), live, foreign)
    assert live.item_id not in superseded_item_ids(items)


def test_no_checkpoint_group_keeps_live_result():
    """确认 carrier 未进入 view（正常形态）→ 不触发过滤。"""
    items = (_stream_reasoning(), _stream_tool_call(), _live_tool_result())
    assert superseded_item_ids(items) == set()
    messages = project_canonical_items(items, preserve_order=True)
    assert any(type(message).__name__ == "ToolMessage" for message in messages)


@pytest.fixture
def protected_reasoning_carrier():
    block = {
        "type": "reasoning_items",
        "reasoning_items": [{
            "type": "reasoning", "id": "rs-vendor",
            "summary": [{"index": 0, "type": "summary_text", "text": ""}],
            "content": [{"type": "reasoning_text", "text": "用户要求委派子代理。"}],
            "encrypted_content": "encrypted-reasoning-fixture",
            "provider_extension": {"trace_group": "vendor-group"},
        }],
    }
    item = _item(
        4, "item-resp-vendor-content-0", SemanticKind.REASONING, PayloadKind.EXTENSION,
        {
            "extension_schema": "boxteam.checkpoint.reasoning-carrier",
            "extension_version": "1", "encoding": "json", "wire_type": "reasoning_items",
            "schema_version": "1", "value": block,
            "protection": {"visibility": "protected"},
        },
        wire_role="assistant", message_group_id=f"message-lc_run--{MODEL_CALL}",
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL),
        projection_group=ProjectionGroup(0, 2, "list"),
    )
    return block, item


def test_protected_reasoning_replays_with_one_emitting_call_and_result(
    protected_reasoning_carrier,
):
    block, reasoning = protected_reasoning_carrier
    items = (
        _stream_reasoning(), _stream_tool_call(), _live_tool_result(),
        reasoning, _confirmed_tool_call(), _request_tool_result(),
    )
    losses: list[str] = []
    messages = project_canonical_items(items, capability_losses=losses, preserve_order=True)
    assert losses == []
    assert len(messages) == 2
    assistant, result = messages
    assert assistant.content == [block]
    assert [call["id"] for call in assistant.tool_calls] == [TOOL_CALL_ID]
    assert result.tool_call_id == TOOL_CALL_ID
    assert result.content == RESULT_CONTENT
    assert assistant.response_metadata["model_call_id"] == result.response_metadata["model_call_id"] == MODEL_CALL


@pytest.mark.parametrize("field,value", [
    ("extension_schema", "unknown.reasoning-carrier"),
    ("extension_version", "unsupported-version"),
])
def test_unknown_reasoning_extension_stays_unsupported(
    protected_reasoning_carrier, field, value,
):
    _block, reasoning = protected_reasoning_carrier
    body = reasoning.to_dict()
    body["payload"][field] = value
    body["content_hash"] = content_hash(body["payload_kind"], body["payload"])
    unknown = CanonicalItemRecord.from_dict(body)
    losses: list[str] = []
    messages = project_canonical_items((unknown,), capability_losses=losses)
    assert messages == []
    assert losses == [f"{unknown.item_id}:reasoning/extension"]
