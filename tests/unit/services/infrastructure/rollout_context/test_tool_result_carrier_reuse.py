"""tool_result 等价复用不得吞掉 request 侧 carrier 的回归合同。

背景（D1-investigation.md §2.3/§6.1）：委派（task 工具）后的下一次模型请求
装配存在竞态——`ensure_request_items` 想写的 request 侧 tool_result carrier
（带 ``projection_message_id``）曾被 ``append_items`` 的等价复用替换成工具
执行侧 live item（无该身份），且不产生新 commit；随后投影层把 live tool_result
判为 shadow 且没有替代 carrier，provider wire 缺失尾部 ToolMessage。

本文件用真实 ``RolloutStorage``（tmp_path 会话 bundle）固化三类行为：

1. request 侧 carrier + 等价 live item → 不复用，两个 item 都持久化；
2. 双方都无 ``projection_message_id`` 的等价 tool_result → 维持既有复用；
3. 双方都有 ``projection_message_id`` 且等价 → 维持既有复用；
4. 正文冲突的 request carrier → 仍被既有 ``ItemSchemaError`` 防线拒绝。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutStorage,
)

SESSION_ID = "ses_be073696349845c58564f112a3180697"
TURN_ID = "turn-delegated-turn2"
MODEL_CALL_ID = "01a0a5cc-a65d-7dd2-acfb-9dad99508516"
PROVIDER_TOOL_CALL_ID = "child-thread-task-call"
SCOPED_TOOL_CALL_ID = f"{MODEL_CALL_ID}:tool-call:{PROVIDER_TOOL_CALL_ID}"
LIVE_EXECUTION_ID = "01a0a5cc-a8a5-77f1-aed0-ecb624dcda2e"
RESULT_CONTENT = '{"child_session_id": "ses_child", "status": "accepted"}'


def _live_tool_result(sequence: int) -> CanonicalItemRecord:
    """工具执行侧 live item：无 projection_message_id（D1 缺陷形态）。"""
    return CanonicalItemRecord.create(
        item_sequence=sequence,
        item_id=f"item-{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": LIVE_EXECUTION_ID,
            "invocation_id": MODEL_CALL_ID,
        },
        payload={
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": (
                f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}"
            ),
            "tool_attempt_id": LIVE_EXECUTION_ID,
            "result_id": LIVE_EXECUTION_ID,
            "name": "task",
            "content": RESULT_CONTENT,
            "tool_outcome": "success",
        },
        metadata={
            "execution_id": LIVE_EXECUTION_ID,
            "model_call_id": MODEL_CALL_ID,
            "tool_execution_id": LIVE_EXECUTION_ID,
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": (
                f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}"
            ),
            "tool_attempt_id": LIVE_EXECUTION_ID,
            "execution_confirmed": True,
        },
        turn_id=TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id=f"message-{LIVE_EXECUTION_ID}",
        wire_role="tool",
    )


def _request_carrier(
    sequence: int,
    *,
    message_id: str,
    content: str = RESULT_CONTENT,
) -> CanonicalItemRecord:
    """request 侧 carrier：ensure_request_items / codec 形态（带投影身份）。"""
    return CanonicalItemRecord.create(
        item_sequence=sequence,
        item_id=f"item-{message_id}",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": message_id,
            "invocation_id": TURN_ID,
        },
        payload={
            "tool_call_id": PROVIDER_TOOL_CALL_ID,
            "result_id": message_id,
            "name": "task",
            "content": content,
            "tool_outcome": "success",
        },
        metadata={
            "projection_message_id": message_id,
            "wire_role": "tool",
            "execution_confirmed": True,
            "model_call_id": MODEL_CALL_ID,
            "tool_call_id": PROVIDER_TOOL_CALL_ID,
        },
        turn_id=TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id=f"message-{message_id}",
        wire_role="tool",
    )


@pytest.fixture
def storage(tmp_path: Path, session_bundle_factory) -> RolloutStorage:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    return RolloutStorage(sessions_dir)


def _catalog_tool_result_ids(storage: RolloutStorage) -> list[str]:
    with sqlite3.connect(storage.index_path(SESSION_ID)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT item_id FROM item_catalog "
                "WHERE semantic_kind = 'tool_result' ORDER BY item_sequence"
            )
        ]


def _view_member_ids(storage: RolloutStorage) -> set[str]:
    with sqlite3.connect(storage.index_path(SESSION_ID)) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT item_id FROM context_view_items")
        }


def test_request_carrier_not_reused_into_live_item(storage: RolloutStorage):
    """带投影身份的 request carrier 不得被等价 live item 吞掉。"""
    live = _live_tool_result(sequence=1)
    carrier = _request_carrier(sequence=2, message_id="1b56a352-tool-msg")
    live_commits = storage.append_items(SESSION_ID, (live,))
    carrier_commits = storage.append_items(SESSION_ID, (carrier,))
    # 两个 item 都在 catalog：carrier 没有被“等价复用”成 live item。
    assert _catalog_tool_result_ids(storage) == [
        live.item_id,
        carrier.item_id,
    ]
    # carrier 产生独立的新 commit（旧缺陷路径不会产生新 commit）。
    assert carrier_commits and not set(carrier_commits) & set(live_commits)
    # carrier 立即进入 active view，下一次装配才能读到它。
    members = _view_member_ids(storage)
    assert live.item_id in members
    assert carrier.item_id in members
    # 两个 item 都能按 catalog locator 完整回读。
    stored = {
        item.item_id: item
        for item in storage.read_items(SESSION_ID)
        if item.semantic_kind == SemanticKind.TOOL_RESULT
    }
    assert set(stored) == {live.item_id, carrier.item_id}
    assert stored[carrier.item_id].metadata["projection_message_id"] == (
        "1b56a352-tool-msg"
    )
    assert "projection_message_id" not in stored[live.item_id].metadata


def test_equivalent_live_tool_results_still_reused(storage: RolloutStorage):
    """双方都无 projection_message_id 时维持既有复用行为。"""
    first = _live_tool_result(sequence=1)
    second = CanonicalItemRecord.create(
        item_sequence=2,
        item_id="item-other-execution-result-" + SCOPED_TOOL_CALL_ID,
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind=PayloadKind.TOOL_RESULT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "tool",
            "producer_id": "other-execution",
            "invocation_id": MODEL_CALL_ID,
        },
        payload={
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": (
                f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}"
            ),
            "tool_attempt_id": "other-execution",
            "result_id": "other-execution",
            "name": "task",
            "content": RESULT_CONTENT,
            "tool_outcome": "success",
        },
        metadata={
            "execution_id": "other-execution",
            "model_call_id": MODEL_CALL_ID,
            "tool_execution_id": "other-execution",
            "tool_call_id": SCOPED_TOOL_CALL_ID,
            "tool_invocation_id": (
                f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}"
            ),
            "tool_attempt_id": "other-execution",
            "execution_confirmed": True,
        },
        turn_id=TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-other-execution",
        wire_role="tool",
    )
    first_commits = storage.append_items(SESSION_ID, (first,))
    second_commits = storage.append_items(SESSION_ID, (second,))
    # 第二个等价 live item 被复用：catalog 仍只有一个 tool_result。
    assert _catalog_tool_result_ids(storage) == [first.item_id]
    # 复用不产生新 commit：返回的是第一次 append 的 commit id。
    assert second_commits == first_commits


def test_equivalent_request_carriers_still_reused(storage: RolloutStorage):
    """双方都有 projection_message_id 且等价时维持既有复用行为。

    复刻“固定信封与信封内部目标工具各产生一个 checkpoint shadow”的场景：
    两个 carrier 共享同一次 provider tool call 且正文一致，只有 result_id
    （producer 生命周期身份）不同。
    """
    envelope_carrier = _request_carrier(
        sequence=1, message_id="msg-envelope-shadow"
    )
    target_carrier = _request_carrier(
        sequence=2, message_id="msg-target-shadow"
    )
    first_commits = storage.append_items(SESSION_ID, (envelope_carrier,))
    second_commits = storage.append_items(SESSION_ID, (target_carrier,))
    assert _catalog_tool_result_ids(storage) == [envelope_carrier.item_id]
    assert second_commits == first_commits


def test_conflicting_request_carrier_still_rejected(storage: RolloutStorage):
    """同 call_id 正文冲突的 request carrier 仍被等价校验拒绝。

    判别位于正文等价校验之后：修复只改变“等价通过后”的处置，
    不削弱既有的冲突防线。
    """
    live = _live_tool_result(sequence=1)
    conflicting = _request_carrier(
        sequence=2,
        message_id="msg-conflicting",
        content='{"child_session_id": "ses_other", "status": "failed"}',
    )
    storage.append_items(SESSION_ID, (live,))
    with pytest.raises(ItemSchemaError, match="tool_result 正文发生变化"):
        storage.append_items(SESSION_ID, (conflicting,))
    assert _catalog_tool_result_ids(storage) == [live.item_id]
