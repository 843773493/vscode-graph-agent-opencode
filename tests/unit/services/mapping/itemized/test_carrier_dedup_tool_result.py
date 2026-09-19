"""carrier_dedup 的 tool_result shadow 判定 fail-loud 合同。

背景（D1-investigation.md §2.4/§6.2）：``superseded_stream_tool_group_item_ids``
把 live tool_result 判为 shadow 时，注释假定 checkpoint 的完整 result carrier
会被保留；若 selection 里根本没有该 carrier（D1 缺陷：request 侧 carrier 被
等价复用吞掉），旧实现会静默产出缺失尾部 ToolMessage 的投影。修复后的合同：

1. 过滤将删除唯一 tool_result 且无替代 carrier → 显式 ``ValueError``；
2. 存在带投影身份的替代 carrier → 照常过滤 live shadow，carrier 保留；
3. 无 checkpoint 组（确认 carrier 未进入 view）→ 不触发任何过滤。
"""

from __future__ import annotations

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.carrier_dedup import (
    superseded_stream_item_ids,
    superseded_stream_tool_group_item_ids,
)
from app.services.mapping.itemized.langchain import project_canonical_items

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
    metadata: dict[str, object],
    *,
    wire_role: str,
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
        metadata=metadata,
        payload=payload,
    )


def _stream_reasoning(sequence: int = 1) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-{EXECUTION}-model-{MODEL_CALL}-block-{MODEL_CALL}:block:part_reasoning",
        SemanticKind.REASONING,
        PayloadKind.TEXT,
        "用户要求委派子代理。",
        {
            "block_id": f"{MODEL_CALL}:block:part_reasoning",
            "block_index": 0,
            "execution_id": EXECUTION,
            "model_call_id": MODEL_CALL,
        },
        wire_role="assistant",
        message_group_id=f"message-{MODEL_CALL}",
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
        {
            "block_id": SCOPED_TOOL_CALL_ID,
            "block_index": 0,
            "execution_id": EXECUTION,
            "model_call_id": MODEL_CALL,
            "tool_invocation_id": f"tool-invocation:{MODEL_CALL}:{TOOL_CALL_ID}",
        },
        wire_role="assistant",
        message_group_id=f"message-{MODEL_CALL}",
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
        },
        {
            "execution_confirmed": True,
            "execution_id": LIVE_EXECUTION_ID,
            "model_call_id": MODEL_CALL,
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": f"tool-invocation:{MODEL_CALL}:{TOOL_CALL_ID}",
        },
        wire_role="tool",
        message_group_id=f"message-{LIVE_EXECUTION_ID}",
    )


def _confirmed_reasoning(sequence: int = 4) -> CanonicalItemRecord:
    return _item(
        sequence,
        f"item-lc_run--{MODEL_CALL}-content-0",
        SemanticKind.REASONING,
        PayloadKind.TEXT,
        "用户要求委派子代理。",
        {
            "execution_confirmed": True,
            "model_call_id": MODEL_CALL,
            "projection_group": {"content_form": "list", "ordinal": 0, "size": 2},
            "reasoning_carrier": {
                "text_key": "reasoning_content",
                "type": "reasoning_content",
            },
            "wire_role": "assistant",
        },
        wire_role="assistant",
        message_group_id=f"message-lc_run--{MODEL_CALL}",
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
        {
            "execution_confirmed": True,
            "model_call_id": MODEL_CALL,
            "projection_group": {"content_form": "list", "ordinal": 1, "size": 2},
            "projection_message_id": f"lc_run--{MODEL_CALL}",
            "wire_role": "assistant",
        },
        wire_role="assistant",
        message_group_id=f"message-lc_run--{MODEL_CALL}",
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
        },
        {
            "execution_confirmed": True,
            "model_call_id": MODEL_CALL,
            "projection_message_id": "1b56a352-af40-4db3-adc2-0a77994e039e",
            "tool_call_id": TOOL_CALL_ID,
            "wire_role": "tool",
        },
        wire_role="tool",
        message_group_id="message-1b56a352-af40-4db3-adc2-0a77994e039e",
    )


def test_unique_tool_result_deletion_raises():
    """确认 carrier 已进 view 但无替代 result carrier → 显式报错。"""
    items = (
        _stream_reasoning(),
        _stream_tool_call(),
        _live_tool_result(),
        _confirmed_reasoning(),
        _confirmed_tool_call(),
    )
    with pytest.raises(ValueError, match="唯一的 tool_result carrier"):
        superseded_stream_tool_group_item_ids(items)
    with pytest.raises(ValueError, match="唯一的 tool_result carrier"):
        superseded_stream_item_ids(items)


def test_live_result_filtered_only_with_replacement_carrier():
    """存在带投影身份的替代 carrier 时照常过滤 live shadow。"""
    items = (
        _stream_reasoning(),
        _stream_tool_call(),
        _live_tool_result(),
        _confirmed_reasoning(),
        _confirmed_tool_call(),
        _request_tool_result(),
    )
    superseded = superseded_stream_item_ids(items)
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


def test_no_checkpoint_group_keeps_live_result():
    """确认 carrier 未进入 view（正常形态）→ 不触发过滤。"""
    items = (_stream_reasoning(), _stream_tool_call(), _live_tool_result())
    assert superseded_stream_item_ids(items) == set()
    messages = project_canonical_items(items, preserve_order=True)
    assert any(type(message).__name__ == "ToolMessage" for message in messages)
