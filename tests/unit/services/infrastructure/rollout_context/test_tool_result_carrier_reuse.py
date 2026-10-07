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
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutStorage,
)

SESSION_ID = "ses_019c3c97527e707e8dc56fca3f021b97"
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
            "tool_outcome": "success"
        , "execution_confirmed": True},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
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
            "tool_outcome": "success"
        , "execution_confirmed": True},
        projection_identity=ItemProjectionIdentity(projection_message_id=message_id, model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id=f"message-{message_id}",
        wire_role="tool",
    )


@pytest.fixture
def storage(tmp_path: Path, session_bundle_factory) -> RolloutStorage:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    owner = RolloutStorage(sessions_dir, message_codec=LangChainMessageCodec())
    owner.accept_turn(
        SESSION_ID, thread_id=owner.main_thread_id(SESSION_ID),
        accepted_ingress_id="carrier-ingress", acceptance_idempotency_key="carrier-accept",
        job_id="carrier-job", admission_idempotency_key="carrier-admission",
        payload="工具结果复用合同", turn_id=TURN_ID, initial_execution_id=MODEL_CALL_ID,
    )
    return owner


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
    live_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (live,), execution_id=MODEL_CALL_ID)
    carrier_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (carrier,), execution_id=MODEL_CALL_ID)
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
        for item in storage.read_items(SESSION_ID, storage.main_thread_id(SESSION_ID))
        if item.semantic_kind == SemanticKind.TOOL_RESULT
    }
    assert set(stored) == {live.item_id, carrier.item_id}
    assert stored[carrier.item_id].projection_identity.projection_message_id == (
        "1b56a352-tool-msg"
    )
    assert stored[live.item_id].projection_identity.projection_message_id is None


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
            "tool_outcome": "success"
        , "execution_confirmed": True},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id="message-other-execution",
        wire_role="tool",
    )
    first_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (first,), execution_id=MODEL_CALL_ID)
    second_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (second,), execution_id=MODEL_CALL_ID)
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
    first_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (envelope_carrier,), execution_id=MODEL_CALL_ID)
    second_commits = storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (target_carrier,), execution_id=MODEL_CALL_ID)
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
    storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (live,), execution_id=MODEL_CALL_ID)
    with pytest.raises(ItemSchemaError, match="tool_result 正文发生变化"):
        storage.append_items(SESSION_ID, storage.main_thread_id(SESSION_ID), (conflicting,), execution_id=MODEL_CALL_ID)
    assert _catalog_tool_result_ids(storage) == [live.item_id]


@pytest.fixture
def carrier_saver(storage: RolloutStorage) -> Iterator[RolloutCheckpointSaver]:
    with RolloutCheckpointSaver(storage.sessions_dir, storage=storage) as saver:
        yield saver


@pytest.mark.parametrize("result_order, expected_stream_result", [
    ("stream_first", f"{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}"),
    ("checkpoint_first", "request-result"),
])
def test_tool_pairing_survives_selected_carrier_replacement(
    storage: RolloutStorage, carrier_saver: RolloutCheckpointSaver, result_order: str,
    expected_stream_result: str,
) -> None:
    """两个 sink 的真实结果按任意顺序提交后，所选 checkpoint 工具组仍闭合。"""
    thread_id = storage.main_thread_id(SESSION_ID)
    stream_call = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-live-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "live-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_call_id": SCOPED_TOOL_CALL_ID, "name": "task", "args": {}},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-live-call",
        wire_role="assistant",
    )
    checkpoint_call = CanonicalItemRecord.create(
        item_sequence=2, item_id="item-request-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "request-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_calls": [{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}]},
        projection_identity=ItemProjectionIdentity(
            projection_message_id="request-call", model_call_id=MODEL_CALL_ID,
        ),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-request-call",
        wire_role="assistant",
    )
    for call in (stream_call, checkpoint_call):
        storage.append_items(SESSION_ID, thread_id, (call,), execution_id=MODEL_CALL_ID)
    stream_result = _live_tool_result(3)
    checkpoint_result = _request_carrier(4, message_id="request-result")
    ordered_results = (
        (stream_result, checkpoint_result) if result_order == "stream_first"
        else (checkpoint_result, stream_result)
    )
    for result in ordered_results:
        storage.append_items(SESSION_ID, thread_id, (result,), execution_id=MODEL_CALL_ID)

    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        sequences = carrier_saver.selected_context_message_sequences(snapshot)
        selected_messages = {
            message_id
            for sequence, message_id in snapshot.connection.execute(
                "SELECT message_sequence, message_id FROM messages"
            )
            if sequence in sequences
        }
        assert "request-call" in selected_messages
        assert "request-result" in selected_messages
        assert "live-call" not in selected_messages
        assert f"{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}" not in selected_messages
        validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
        all_sequences = [row[0] for row in snapshot.connection.execute(
            "SELECT message_sequence FROM messages ORDER BY message_sequence"
        )]
        validate_tool_protocol_closure(snapshot.connection, all_sequences, len(all_sequences))
        pairings = dict(snapshot.connection.execute(
            "SELECT call.tool_call_id, result.message_id FROM tool_calls AS call "
            "JOIN messages AS result ON result.message_sequence = call.result_message_sequence"
        ))
        assert pairings == {
            SCOPED_TOOL_CALL_ID: expected_stream_result,
            PROVIDER_TOOL_CALL_ID: "request-result",
        }
