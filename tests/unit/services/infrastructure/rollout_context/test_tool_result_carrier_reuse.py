"""tool_result 等价复用不得吞掉 request 侧 carrier 的回归合同。

背景（D1-investigation.md §2.3/§6.1）：委派（task 工具）后的下一次模型请求
装配存在竞态——`ensure_request_items` 想写的 request 侧 tool_result carrier
（带 ``projection_message_id``）曾被 ``append_items`` 的等价复用替换成工具
执行侧 live item（无该身份），且不产生新 commit；随后投影层把 live tool_result
判为 shadow 且没有替代 carrier，provider wire 缺失尾部 ToolMessage。

本文件用真实 ``RolloutContextStore``（tmp_path 会话 bundle）固化三类行为：

1. request 侧 carrier 与等价 live item 任意顺序到达 → 两个 item 都持久化；
2. 双方都无 ``projection_message_id`` 的等价 tool_result → 维持既有复用；
3. 双方都有 ``projection_message_id`` 且等价 → 维持既有复用；
4. 正文冲突的 request carrier → 仍被既有 ``ItemSchemaError`` 防线拒绝。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.agents.summarization.planning import build_cache_preserving_event
from app.agents.summarization.projection import apply_summarization_event
from app.agents.summarization.state import CachePreservingPartition
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    CONFLICT_CODE,
    ToolProtocolBoundaryConflict,
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.store import (
    RolloutContextStore,
)

SESSION_ID = "ses_019c3c97527e707e8dc56fca3f021b97"
TURN_ID = "turn-delegated-turn2"
MODEL_CALL_ID = "01a0a5cc-a65d-7dd2-acfb-9dad99508516"
PROVIDER_TOOL_CALL_ID = "child-thread-task-call"
SCOPED_TOOL_CALL_ID = f"{MODEL_CALL_ID}:tool-call:{PROVIDER_TOOL_CALL_ID}"
LIVE_EXECUTION_ID = "01a0a5cc-a8a5-77f1-aed0-ecb624dcda2e"
RESULT_CONTENT = '{"child_session_id": "ses_child", "status": "accepted"}'


def _live_tool_result(sequence: int, *, outcome: str = "success") -> CanonicalItemRecord:
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
            "tool_outcome": outcome,
            "execution_confirmed": outcome != "unknown",
        },
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
            "invocation_id": MODEL_CALL_ID,
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
def storage(tmp_path: Path, session_bundle_factory) -> RolloutContextStore:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    owner = RolloutContextStore(sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec())
    owner.executions.accept_turn(
        SESSION_ID, thread_id=owner.storage.main_thread_id(SESSION_ID),
        accepted_ingress_id="carrier-ingress", acceptance_idempotency_key="carrier-accept",
        job_id="carrier-job", admission_idempotency_key="carrier-admission",
        payload="工具结果复用合同", turn_id=TURN_ID, initial_execution_id=MODEL_CALL_ID,
    )
    return owner


def _catalog_tool_result_ids(storage: RolloutContextStore) -> list[str]:
    with sqlite3.connect(storage.storage.index_path(SESSION_ID)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT item_id FROM item_catalog "
                "WHERE semantic_kind = 'tool_result' ORDER BY item_sequence"
            )
        ]


def _view_member_ids(storage: RolloutContextStore) -> set[str]:
    with sqlite3.connect(storage.storage.index_path(SESSION_ID)) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT item_id FROM context_view_items")
        }


def test_request_carrier_not_reused_into_live_item(storage: RolloutContextStore):
    """带投影身份的 request carrier 不得被等价 live item 吞掉。"""
    live = _live_tool_result(sequence=1)
    carrier = _request_carrier(sequence=2, message_id="1b56a352-tool-msg")
    live_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (live,), execution_id=MODEL_CALL_ID)
    carrier_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (carrier,), execution_id=MODEL_CALL_ID)
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
        for item in storage.storage.read_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID))
        if item.semantic_kind == SemanticKind.TOOL_RESULT
    }
    assert set(stored) == {live.item_id, carrier.item_id}
    assert stored[carrier.item_id].projection_identity.projection_message_id == (
        "1b56a352-tool-msg"
    )
    assert stored[live.item_id].projection_identity.projection_message_id is None


def test_equivalent_live_tool_results_still_reused(storage: RolloutContextStore):
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
    first_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (first,), execution_id=MODEL_CALL_ID)
    second_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (second,), execution_id=MODEL_CALL_ID)
    # 第二个等价 live item 被复用：catalog 仍只有一个 tool_result。
    assert _catalog_tool_result_ids(storage) == [first.item_id]
    # 复用不产生新 commit：返回的是第一次 append 的 commit id。
    assert second_commits == first_commits


def test_equivalent_request_carriers_still_reused(storage: RolloutContextStore):
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
    first_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (envelope_carrier,), execution_id=MODEL_CALL_ID)
    second_commits = storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (target_carrier,), execution_id=MODEL_CALL_ID)
    assert _catalog_tool_result_ids(storage) == [envelope_carrier.item_id]
    assert second_commits == first_commits


def test_conflicting_request_carrier_still_rejected(storage: RolloutContextStore):
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
    storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (live,), execution_id=MODEL_CALL_ID)
    with pytest.raises(ItemSchemaError, match="tool_result 正文发生变化"):
        storage.executions.append_items(SESSION_ID, storage.storage.main_thread_id(SESSION_ID), (conflicting,), execution_id=MODEL_CALL_ID)
    assert _catalog_tool_result_ids(storage) == [live.item_id]


@pytest.fixture
def carrier_saver(storage: RolloutContextStore) -> Iterator[RolloutCheckpointSaver]:
    with RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage) as saver:
        yield saver


@pytest.fixture
def tool_declarations(storage: RolloutContextStore) -> str:
    """通过真实提交入口保存同一调用的两个 carrier 声明。"""
    return _persist_tool_declarations(storage)


def _persist_tool_declarations(storage: RolloutContextStore) -> str:
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    stream_call = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-live-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "live-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_call_id": SCOPED_TOOL_CALL_ID, "name": "task", "args": {}},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-live-call",
        wire_role="assistant",
    )
    checkpoint_call, = LangChainMessageCodec().items_for_message(
        AIMessage(content="", id="request-call", response_metadata={"model_call_id": MODEL_CALL_ID},
                  tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}]),
        item_sequence=2, message_id="request-call", turn_id=TURN_ID,
        execution_id=MODEL_CALL_ID, timestamp=stream_call.created_at,
    )
    for call in (stream_call, checkpoint_call):
        storage.executions.append_items(SESSION_ID, thread_id, (call,), execution_id=MODEL_CALL_ID)
    return thread_id


@pytest.fixture
def tool_result_owner(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    frozen_provider_activation, frozen_provider_wire,
) -> tuple[str, ToolResultCompletion]:
    """先走真实 prepare/ModelCall 登记，再保存同一调用的两份声明。"""
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    admission = carrier_saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID)
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(SESSION_ID, thread_id), control_state_port=carrier_saver,
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        prepared = carrier_saver.prepare_context_for_provider(
            SESSION_ID, thread_id, turn_id=TURN_ID, execution_id=MODEL_CALL_ID,
            plan_creation_idempotency_key="terminal-owner-prepare", seal_idempotency_key="terminal-owner-seal",
            context_source_manager=manager, tool_set_supplier=lambda: selected,
            **frozen_provider_wire("terminal-owner", "chat_completions"),
            **frozen_provider_activation(carrier_saver, plan_creation_idempotency_key="terminal-owner-prepare", model_call_id=MODEL_CALL_ID),
        )
    carrier_saver.register_model_call(
        SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID,
        model_call_id=MODEL_CALL_ID, attempt=1, provider="terminal-owner",
        assembly_id=prepared["assembly_id"], dispatch_state="dispatched",
    )
    assert _persist_tool_declarations(storage) == thread_id
    return thread_id, ToolResultCompletion(
        model_call_id=MODEL_CALL_ID, tool_call_id=SCOPED_TOOL_CALL_ID,
        tool_invocation_id=f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}",
        tool_attempt_id=LIVE_EXECUTION_ID, tool_name="task",
        content=RESULT_CONTENT, tool_outcome="success",
    )


def test_tool_result_owner_retries_only_exact_original_attempt(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_result_owner,
) -> None:
    thread_id, completion = tool_result_owner
    first = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=completion,
    )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    second = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=completion,
    )
    assert second.to_dict() == first.to_dict()
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes
    another = replace(completion, tool_attempt_id="another-real-attempt")
    with pytest.raises(RuntimeError, match="同一 tool_call_id 已绑定其他工具终态 attempt"):
        carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=another,
        )
    assert _catalog_tool_result_ids(storage) == [first.item_id]
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


@pytest.mark.parametrize("result_order", ["request_first", "live_first"])
def test_tool_result_owner_keeps_request_projection_and_original_attempt(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_result_owner,
    result_order: str,
) -> None:
    """两个合法到达顺序都保 P/A，后继真实请求复用不引入第三个结果。"""
    thread_id, completion = tool_result_owner
    messages = [
        AIMessage(content="", id="request-call", response_metadata={"model_call_id": MODEL_CALL_ID},
                  tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}]),
        ToolMessage(content=RESULT_CONTENT, id="request-owner-result", name="task", tool_call_id=PROVIDER_TOOL_CALL_ID),
    ]

    def prepare_request() -> None:
        carrier_saver.ensure_request_items(
            SESSION_ID, thread_id, turn_id=TURN_ID, execution_id=MODEL_CALL_ID, messages=messages,
        )

    if result_order == "request_first":
        prepare_request()
    first = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=completion,
    )
    prepare_request()
    results = [item for item in storage.storage.read_items(SESSION_ID, thread_id) if item.semantic_kind == "tool_result"]
    assert {item.item_id for item in results} == {completion.item_id, "item-request-owner-result"}
    carrier, = [item for item in results if item.item_id != completion.item_id]
    assert carrier.projection_identity == ItemProjectionIdentity(projection_message_id=messages[-1].id, model_call_id=MODEL_CALL_ID)
    assert "tool_attempt_id" not in carrier.payload
    assert first.item_id == completion.item_id
    assert first.payload["tool_attempt_id"] == LIVE_EXECUTION_ID
    with storage.storage.read_connection(SESSION_ID, thread_id) as connection:
        result_commits = dict(connection.execute(
            "SELECT item_id,commit_id FROM item_catalog WHERE semantic_kind='tool_result'",
        ))
    assert result_commits[first.item_id] != result_commits[carrier.item_id]
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    recovered = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job",
        completion=replace(completion, content="冷恢复未知通知", tool_outcome="unknown"), recovery=True,
    )
    assert recovered.to_dict() == first.to_dict()
    prepare_request()
    with pytest.raises(RuntimeError, match="其他工具终态 attempt"):
        carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job",
            completion=replace(completion, tool_attempt_id="another-real-attempt"),
        )
    with pytest.raises(ItemSchemaError, match="正文发生变化"):
        carrier_saver.ensure_request_items(
            SESSION_ID, thread_id, turn_id=TURN_ID, execution_id=MODEL_CALL_ID,
            messages=[messages[0], messages[-1].model_copy(update={"id": "conflicting-request-result", "content": "冲突正文"})],
        )
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


@pytest.mark.parametrize("changed", ["tool_invocation_id", "content", "tool_outcome"])
def test_tool_result_owner_rejects_same_attempt_identity_or_payload_changes(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_result_owner,
    changed: str,
) -> None:
    thread_id, completion = tool_result_owner
    first = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=completion,
    )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    changed_completion = replace(completion, **{changed: "failure" if changed == "tool_outcome" else "冲突内容"})
    with pytest.raises((RuntimeError, ItemSchemaError), match="身份冲突|immutable identity/status/payload 冲突"):
        carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=changed_completion,
        )
    assert _catalog_tool_result_ids(storage) == [first.item_id]
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


def test_tool_result_owner_never_reattributes_old_model_to_followup_admission(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_result_owner,
) -> None:
    thread_id, completion = tool_result_owner
    first = carrier_saver.commit_tool_result(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", completion=completion,
    )
    carrier_saver.mark_job_terminal_status(
        session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job", status="execution_lost",
    )
    storage.executions.accept_turn(
        SESSION_ID, thread_id=thread_id, accepted_ingress_id="terminal-owner-next-ingress",
        acceptance_idempotency_key="terminal-owner-next-accept", job_id="terminal-owner-next-job",
        admission_idempotency_key="terminal-owner-next-admission", payload="后继准入",
        turn_id="terminal-owner-next-turn", initial_execution_id="terminal-owner-next-execution",
    )
    following = carrier_saver.execution_admission_for_job(
        SESSION_ID, thread_id=thread_id, job_id="terminal-owner-next-job",
    )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(following, langgraph_thread_id=thread_id),
    ):
        recovered = carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=thread_id, job_id="carrier-job",
            completion=replace(completion, tool_outcome="unknown", content="重启未知通知"), recovery=True,
        )
        assert recovered.to_dict() == first.to_dict()
        with pytest.raises(RuntimeError, match="ModelCall 不属于原 execution"):
            carrier_saver.commit_tool_result(
                session_id=SESSION_ID, thread_id=thread_id, job_id=following.job_id, completion=completion,
            )
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


def test_tool_result_owner_requires_registered_original_model(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_declarations: str,
) -> None:
    completion = ToolResultCompletion(
        model_call_id=MODEL_CALL_ID, tool_call_id=SCOPED_TOOL_CALL_ID,
        tool_invocation_id=f"tool-invocation:{MODEL_CALL_ID}:{PROVIDER_TOOL_CALL_ID}",
        tool_attempt_id=LIVE_EXECUTION_ID, tool_name="task", content=RESULT_CONTENT, tool_outcome="unknown",
    )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=tool_declarations).read_bytes()
    with pytest.raises(RuntimeError, match="ModelCall 不属于原 execution"):
        carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=tool_declarations, job_id="carrier-job", completion=completion, recovery=True,
        )
    assert _catalog_tool_result_ids(storage) == []
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=tool_declarations).read_bytes() == committed_bytes


@pytest.fixture(params=["before_checkpoint", "after_checkpoint"])
def compaction_carrier_checkpoint(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    request: pytest.FixtureRequest,
) -> tuple[str, RunnableConfig, list[BaseMessage]]:
    """真实双声明 carrier，覆盖 terminal 两种到达顺序，再恢复冻结 state。"""
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    codec = storage.storage._codec()
    root = storage.storage.read_items(SESSION_ID, thread_id)[0]
    user = codec.from_dict(codec.project_message((root,)))
    stream_call = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-live-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "live-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_call_id": SCOPED_TOOL_CALL_ID, "name": "task", "args": {}},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-live-call",
        wire_role="assistant",
    )
    storage.executions.append_items(SESSION_ID, thread_id, (stream_call,), execution_id=MODEL_CALL_ID)
    if request.param == "before_checkpoint":
        storage.executions.append_items(SESSION_ID, thread_id, (_live_tool_result(3),), execution_id=MODEL_CALL_ID)
    messages = [
        user,
        AIMessage(content="", id="request-call", response_metadata={"model_call_id": MODEL_CALL_ID},
                  tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}]),
        ToolMessage(content=RESULT_CONTENT, id="request-result", name="task", tool_call_id=PROVIDER_TOOL_CALL_ID,
                    response_metadata={"model_call_id": MODEL_CALL_ID}),
        AIMessage(content="工具已完成，保留近期回答。", id="carrier-final"),
    ]
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "compaction-carrier-source"
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    admission = carrier_saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID)
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        config = carrier_saver.put(build_checkpoint_config(thread_id), checkpoint, {"source": "unit"}, {"messages": "1"})
    if request.param == "after_checkpoint":
        with storage.storage.read_connection(SESSION_ID, thread_id) as connection:
            terminal_commit = connection.execute(
                "SELECT commit_id FROM item_catalog WHERE item_id='item-request-result'",
            ).fetchone()[0]
        appended = storage.executions.append_items(SESSION_ID, thread_id, (_live_tool_result(3),), execution_id=MODEL_CALL_ID)
        assert appended and terminal_commit not in appended
    expected_terminals = (
        [_live_tool_result(3).item_id, "item-request-result"]
        if request.param == "before_checkpoint"
        else ["item-request-result", _live_tool_result(3).item_id]
    )
    assert _catalog_tool_result_ids(storage) == expected_terminals
    with session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id):
        restored = carrier_saver.get_tuple(config)
    assert restored is not None
    state = restored.checkpoint["channel_values"]["messages"]
    assert [message.id for message in state] == [message.id for message in messages]
    assert state[2].content == RESULT_CONTENT
    assert state[2].tool_call_id == PROVIDER_TOOL_CALL_ID
    return thread_id, config, state


@pytest.mark.parametrize("checkpoint_ns", ["", "model:compaction-call"])
def test_compaction_preflight_uses_selected_checkpoint_carriers(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    compaction_carrier_checkpoint: tuple[str, RunnableConfig, list[BaseMessage]],
    checkpoint_ns: str,
) -> None:
    thread_id, _config, state = compaction_carrier_checkpoint
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        before = tuple(snapshot.connection.iterdump())
        checkpoint = storage.checkpoints.latest_checkpoint(
            SESSION_ID, thread_id, snapshot.checkpoint_ns, None, snapshot=snapshot,
        )
        assert checkpoint is not None
        cold_state = storage.checkpoints.checkpoint_values(
            SESSION_ID, thread_id, snapshot.checkpoint_ns, checkpoint,
            snapshot=snapshot, include_execution_context=True,
        )["messages"]
        assert cold_state == state
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        selected = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        raw = storage.storage._view_message_sequences_from_connection(
            snapshot.connection, storage.checkpoints.active_view_id(snapshot),
        )
        assert raw != selected
        rows = dict(snapshot.connection.execute("SELECT message_id, message_sequence FROM messages"))
        assert selected == [rows[message.id] for message in state]
    jsonl = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    before_jsonl = jsonl.read_bytes()
    # state 中的 call/result 位于 1/2，实际持久序号之间还包含两个 sink 的 shadow。
    assert carrier_saver.safe_compaction_prefix_cutoffs(
        SESSION_ID, thread_id, checkpoint_ns=checkpoint_ns,
        state_messages=state, cutoff_indexes=[0, 1, 2, 3, 4],
    ) == frozenset({0, 1, 3, 4})
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


@pytest.mark.parametrize("damage", ["duplicate", "reorder", "gap", "shadow", "inflight_gap"])
def test_compaction_preflight_rejects_invalid_state_prefix(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    compaction_carrier_checkpoint: tuple[str, RunnableConfig, list[BaseMessage]],
    damage: str,
) -> None:
    thread_id, _config, state = compaction_carrier_checkpoint
    invalid = {
        "duplicate": [state[0], state[0], *state[1:]],
        "reorder": [state[0], state[2], state[1], state[3]],
        "gap": [state[0], *state[2:]],
        "shadow": [state[0], AIMessage(content="", id="live-call"), *state[1:]],
        "inflight_gap": [state[0], AIMessage(content="", id="not-yet-persisted"), *state[1:]],
    }[damage]
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        before = tuple(snapshot.connection.iterdump())
    jsonl = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    before_jsonl = jsonl.read_bytes()
    with pytest.raises(RuntimeError, match="compaction preflight 映射损坏"):
        carrier_saver.safe_compaction_prefix_cutoffs(
            SESSION_ID, thread_id, checkpoint_ns="", state_messages=invalid,
            cutoff_indexes=[len(invalid)],
        )
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


def test_compaction_preflight_does_not_restore_hidden_declarations(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    compaction_carrier_checkpoint: tuple[str, RunnableConfig, list[BaseMessage]],
) -> None:
    thread_id, _config, state = compaction_carrier_checkpoint
    with sqlite3.connect(storage.storage.index_path(SESSION_ID, thread_id=thread_id)) as connection:
        view_id = connection.execute(
            "SELECT head_view_id FROM branches WHERE status='active'",
        ).fetchone()[0]
        connection.execute(
            "UPDATE context_view_items SET visible=0 WHERE view_id=? "
            "AND item_id IN ('item-live-call','item-request-call')", (view_id,),
        )
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        selected = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        ids = {sequence: message_id for sequence, message_id in snapshot.connection.execute(
            "SELECT message_sequence, message_id FROM messages",
        )}
        assert "live-call" not in {ids[sequence] for sequence in selected}
        assert "request-call" not in {ids[sequence] for sequence in selected}
        with pytest.raises(ToolProtocolBoundaryConflict, match=CONFLICT_CODE):
            validate_tool_protocol_closure(snapshot.connection, selected, len(selected))
        before = tuple(snapshot.connection.iterdump())
    jsonl = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    before_jsonl = jsonl.read_bytes()
    with pytest.raises(RuntimeError, match="compaction preflight 映射损坏"):
        carrier_saver.safe_compaction_prefix_cutoffs(
            SESSION_ID, thread_id, checkpoint_ns="", state_messages=state,
            cutoff_indexes=[len(state)],
        )
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


@pytest.mark.asyncio
async def test_compaction_checkpoint_preserves_selected_state_cutoff(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    compaction_carrier_checkpoint: tuple[str, RunnableConfig, list[BaseMessage]],
) -> None:
    thread_id, config, state = compaction_carrier_checkpoint
    assert carrier_saver.safe_compaction_prefix_cutoffs(
        SESSION_ID, thread_id, checkpoint_ns="", state_messages=state, cutoff_indexes=[1, 3],
    ) == frozenset({1, 3})
    summary = HumanMessage(content="已完成工具调用，保留真实结果。", id="carrier-summary")
    event = build_cache_preserving_event(
        CachePreservingPartition(prefix_messages=state[:1], messages_to_summarize=state[1:3],
                                 preserved_messages=state[3:], state_cutoff=3),
        summary_message=summary, file_path=str(storage.storage.sessions_dir / "compaction-history.md"),
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "compaction-selected-carrier-cutoff"
    checkpoint["channel_values"] = {"messages": state, "_summarization_event": event}
    checkpoint["channel_versions"] = {"messages": "2", "_summarization_event": "2"}
    checkpoint["updated_channels"] = ["messages", "_summarization_event"]
    admission = carrier_saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID)
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        saved = carrier_saver.put(config, checkpoint, {"source": "compaction"}, checkpoint["channel_versions"])
    with storage.storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
        control = json.loads(snapshot.connection.execute(
            "SELECT payload_json FROM control_events WHERE checkpoint_id=?",
            (checkpoint["id"],),
        ).fetchone()[0])
        cutoff_sequence = snapshot.connection.execute(
            "SELECT message_sequence FROM messages WHERE message_id='request-result'",
        ).fetchone()[0]
        assert control["cutoff_index"] == 3
        assert control["cutoff_message_id"] == "request-result"
        assert control["cutoff_message_sequence"] == cutoff_sequence
        assert cutoff_sequence != control["cutoff_index"]
    with (
        RolloutCheckpointSaver(storage.storage.sessions_dir) as cold,
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
    ):
        restored = cold.get_tuple(saved)
        assert restored is not None
        channels = restored.checkpoint["channel_values"]
        assert channels["messages"] == state
        assert channels["_summarization_event"] == event
        assert apply_summarization_event(channels["messages"], channels["_summarization_event"]) == [state[0], summary, state[3]]
        consumer = MessageService(main_thread_resolver=storage.storage.main_thread_id, checkpointer=cold)
        context = await consumer.get_agent_context_state(SESSION_ID, thread_id)
        assert context["raw_message_count"] == 4
        assert context["compaction_cutoff"] == 3
        assert context["records"] == [
            MessageService._message_to_agent_state_record(message)
            for message in [state[0], summary, state[3]]
        ]


@pytest.mark.parametrize("result_order, expected_stream_result", [
    ("stream_first", f"{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}"),
    ("checkpoint_first", f"{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}"),
    ("checkpoint_only", "request-result"),
])
def test_tool_pairing_survives_selected_carrier_replacement(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_declarations: str,
    result_order: str, expected_stream_result: str,
) -> None:
    """真实结果按任意顺序提交或仅提交 checkpoint 结果，所选工具组均闭合。"""
    thread_id = tool_declarations
    stream_result = _live_tool_result(3)
    checkpoint_result = _request_carrier(4, message_id="request-result")
    ordered_results = {
        "stream_first": (stream_result, checkpoint_result),
        "checkpoint_first": (checkpoint_result, stream_result),
        "checkpoint_only": (checkpoint_result,),
    }[result_order]
    for result in ordered_results:
        storage.executions.append_items(SESSION_ID, thread_id, (result,), execution_id=MODEL_CALL_ID)

    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
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


@pytest.mark.parametrize("source_order", ["hidden_actual_call", "result_before_call", "pending_call"])
def test_shared_result_requires_visible_ordered_actual_declaration(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    tool_declarations: str, source_order: str,
) -> None:
    """共享结果不能用另一 sink 的声明替代被截去或乱序的实际声明。"""
    thread_id = tool_declarations
    if source_order != "pending_call":
        storage.executions.append_items(
            SESSION_ID, thread_id, (_request_carrier(3, message_id="request-result"),),
            execution_id=MODEL_CALL_ID,
        )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        messages = dict(snapshot.connection.execute("SELECT message_id, message_sequence FROM messages"))
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        if source_order == "hidden_actual_call":
            sequences = [messages["live-call"] if sequence == messages["request-call"] else sequence
                         for sequence in sequences]
        elif source_order == "result_before_call":
            call_index = sequences.index(messages["request-call"])
            result_index = sequences.index(messages["request-result"])
            sequences[call_index], sequences[result_index] = sequences[result_index], sequences[call_index]
        before = snapshot.connection.total_changes
        with pytest.raises(ToolProtocolBoundaryConflict) as error:
            validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
        assert error.value.code == CONFLICT_CODE
        assert snapshot.connection.total_changes == before
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


def test_tool_result_without_any_declaration_is_not_closed(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
) -> None:
    """真实 result 已提交但配对查询没有任何行时，仍必须报 orphan 冲突。"""
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.executions.append_items(
        SESSION_ID, thread_id, (_request_carrier(1, message_id="request-result"),),
        execution_id=MODEL_CALL_ID,
    )
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        assert snapshot.connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone() == (0,)
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        before = snapshot.connection.total_changes
        with pytest.raises(ToolProtocolBoundaryConflict) as error:
            validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
        assert error.value.code == CONFLICT_CODE
        assert snapshot.connection.total_changes == before
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


@pytest.mark.parametrize("damaged_field", [
    "model_call_id", "turn_id", "execution_id", "tool_call_id", "projection_message_id",
    "missing_model_call_scope",
])
def test_shared_result_rejects_damaged_hidden_carrier_identity(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    tool_declarations: str, damaged_field: str,
) -> None:
    """故障注入：同 raw ID 的隐藏镜像有任一身份损坏，都不能得到豁免。"""
    thread_id = tool_declarations
    storage.executions.append_items(
        SESSION_ID, thread_id, (_request_carrier(3, message_id="request-result"),),
        execution_id=MODEL_CALL_ID,
    )
    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        items = carrier_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=carrier_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = carrier_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        live_sequence = snapshot.connection.execute(
            "SELECT message_sequence FROM messages WHERE message_id='live-call'",
        ).fetchone()[0]
    assert live_sequence not in sequences
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    with sqlite3.connect(storage.storage.index_path(SESSION_ID, thread_id=thread_id)) as connection:
        if damaged_field == "missing_model_call_scope":
            connection.execute(
                "UPDATE item_catalog SET projection_manifest_json=json_remove("
                "projection_manifest_json, '$.projection_identity.model_call_id') "
                "WHERE item_id IN ('item-live-call', 'item-request-call', 'item-request-result')",
            )
            connection.execute(
                "UPDATE tool_calls SET tool_call_id=? WHERE assistant_message_sequence=?",
                (PROVIDER_TOOL_CALL_ID, live_sequence),
            )
        elif damaged_field in {"model_call_id", "projection_message_id"}:
            connection.execute(
                "UPDATE item_catalog SET projection_manifest_json=json_set(projection_manifest_json, ?, ?) "
                "WHERE item_id='item-live-call'",
                (f"$.projection_identity.{damaged_field}", f"wrong-{damaged_field}"),
            )
        elif damaged_field == "tool_call_id":
            connection.execute(
                "UPDATE tool_calls SET tool_call_id=? WHERE assistant_message_sequence=?",
                ("different-provider-call", live_sequence),
            )
        else:
            connection.execute(
                f"UPDATE messages SET {damaged_field}=? WHERE message_sequence=?",
                (f"wrong-{damaged_field}", live_sequence),
            )
        connection.commit()
        state_before = connection.execute("SELECT * FROM database_meta").fetchall()
        pairs_before = connection.execute("SELECT * FROM tool_calls").fetchall()
        with pytest.raises(ToolProtocolBoundaryConflict) as error:
            validate_tool_protocol_closure(connection, sequences, len(sequences))
        assert error.value.code == CONFLICT_CODE
        assert connection.execute("SELECT * FROM database_meta").fetchall() == state_before
        assert connection.execute("SELECT * FROM tool_calls").fetchall() == pairs_before
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


def test_shared_result_requires_unique_actual_carrier_declaration(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver, tool_declarations: str,
) -> None:
    """真实写入两个实际 carrier 声明，不能按其中任意一个豁免隐藏镜像。"""
    thread_id = tool_declarations
    declaration, = [item for item in storage.storage.read_items(SESSION_ID, thread_id)
                    if item.item_id == "item-request-call"]
    duplicate, = LangChainMessageCodec().items_for_message(
        AIMessage(content="", id="second-request-call", response_metadata={"model_call_id": MODEL_CALL_ID},
                  tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}]),
        item_sequence=3, message_id="second-request-call", turn_id=TURN_ID,
        execution_id=MODEL_CALL_ID, timestamp=declaration.created_at,
    )
    for item in (duplicate, _request_carrier(4, message_id="request-result")):
        storage.executions.append_items(SESSION_ID, thread_id, (item,), execution_id=MODEL_CALL_ID)
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    with carrier_saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        sequences = [sequence for sequence, message_id in snapshot.connection.execute(
            "SELECT message_sequence, message_id FROM messages ORDER BY message_sequence",
        ) if message_id != "live-call"]
        assert snapshot.connection.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE tool_call_id=? AND result_message_sequence IS NOT NULL",
            (PROVIDER_TOOL_CALL_ID,),
        ).fetchone() == (2,)
        before = snapshot.connection.total_changes
        with pytest.raises(ToolProtocolBoundaryConflict) as error:
            validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
        assert error.value.code == CONFLICT_CODE
        assert snapshot.connection.total_changes == before
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == committed_bytes


@pytest.mark.parametrize("tool_outcome", ["failure", "unknown"])
def test_new_execution_restores_preceding_terminal_tool_group(
    storage: RolloutContextStore, tool_outcome: str,
) -> None:
    """失败后冷恢复真实结果，后继准入/配对/历史可读，旧 checkpoint 与字节不变。"""
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    owner = MutationIntentOwner(session_id=SESSION_ID, thread_id=thread_id)
    config = build_checkpoint_config(thread_id)
    stream_call = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-live-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "live-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_call_id": SCOPED_TOOL_CALL_ID, "name": "task", "args": {}},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-live-call",
        wire_role="assistant",
    )
    storage.executions.append_items(SESSION_ID, thread_id, (stream_call,), execution_id=MODEL_CALL_ID)
    root = storage.storage.read_items(SESSION_ID, thread_id)[0]
    user = storage.storage._codec().from_dict(storage.storage._codec().project_message((root,)))
    assistant = AIMessage(
        content="", id="request-call",
        tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}],
        response_metadata={"model_call_id": MODEL_CALL_ID},
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-before-fatal-result"
    checkpoint["channel_values"] = {"messages": [user, assistant]}
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    with RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage) as first:
        admission = first.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID)
        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            with execution_admission_scope(admission, langgraph_thread_id=thread_id):
                fixed = first.put(config, checkpoint, {"source": "unit"}, {"messages": "1"})
            frozen = first.get_tuple(fixed).checkpoint
        storage.executions.append_items(
            SESSION_ID, thread_id, (_live_tool_result(3, outcome=tool_outcome),), execution_id=MODEL_CALL_ID,
        )
        first.converge_execution(
            SESSION_ID, thread_id=thread_id, turn_id=TURN_ID, execution_id=MODEL_CALL_ID,
            outcome="failed", turn_status="failed",
        )
        assert first.bootstrap_history(SESSION_ID, thread_id)[0].turn_id == TURN_ID
    committed_bytes = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
    storage.executions.accept_turn(
        SESSION_ID, thread_id=thread_id, accepted_ingress_id="followup-ingress",
        acceptance_idempotency_key="followup-accept", job_id="followup-job",
        admission_idempotency_key="followup-admission", payload="失败后的合法新输入",
        turn_id="turn-followup", initial_execution_id="execution-followup",
    )
    # 新 Saver 与 ContextStore 均从相同持久事实重建，不消费前一实例的缓存。
    with RolloutCheckpointSaver(storage.storage.sessions_dir) as restored:
        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            admission = restored.execution_admission(SESSION_ID, thread_id=thread_id, execution_id="execution-followup")
            with execution_admission_scope(admission, langgraph_thread_id=thread_id):
                latest = restored.get_tuple(config)
                messages = latest.checkpoint["channel_values"]["messages"]
                assert [message.type for message in messages] == ["human", "ai", "tool", "human"]
                result = messages[2]
                assert result.tool_call_id == PROVIDER_TOOL_CALL_ID
                assert result.id == f"{LIVE_EXECUTION_ID}-result-{SCOPED_TOOL_CALL_ID}"
                assert result.status == "error"
                assert result.content == RESULT_CONTENT
                assert result.response_metadata["tool_outcome"] == tool_outcome
                assert result.response_metadata["execution_confirmed"] is (tool_outcome != "unknown")
                assert PatchToolCallsMiddleware().before_agent({"messages": messages}, None) is None
                restored.ensure_request_items(
                    SESSION_ID, thread_id, turn_id="turn-followup", execution_id="execution-followup",
                    messages=messages,
                )
                with restored._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
                    items = restored._storage.checkpoints.committed_context_items(
                        snapshot, predecessor=restored._storage.assemblies.latest_context_assembly(snapshot),
                    )
                    sequences = restored._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
                    validate_tool_protocol_closure(snapshot.connection, sequences, len(sequences))
                    assert snapshot.connection.execute(
                        "SELECT turn_id, execution_id FROM item_catalog WHERE semantic_kind='tool_result'",
                    ).fetchall() == [(TURN_ID, MODEL_CALL_ID)]
                successor = latest.checkpoint.copy()
                successor["id"] = "checkpoint-followup-with-real-result"
                successor["channel_versions"] = {"messages": "2"}
                restored.put(latest.config, successor, {"source": "unit"}, {"messages": "2"})
                replay = restored.get_tuple(config).checkpoint["channel_values"]["messages"]
                assert [message.type for message in replay] == ["human", "ai", "tool", "human"]
                assert replay[2].content == RESULT_CONTENT
            assert restored.get_tuple(fixed).checkpoint == frozen
        latest_turn, _, _ = restored.bootstrap_history(SESSION_ID, thread_id)
        assert latest_turn.turn_id == "turn-followup"
        original = restored.load_history(
            SESSION_ID, thread_id,
            TurnHistoryLoadRequest(turn_ids=[TURN_ID], include=["user", "tool_call", "tool_result"]),
        ).items[0]
        assert original.status == "failed"
        assert [part.result for part in original.response_parts if part.kind == "tool_result"] == [RESULT_CONTENT]
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes().startswith(committed_bytes)


@pytest.mark.parametrize("stream_result_timing", ["before_checkpoint", "after_checkpoint"])
def test_checkpoint_restore_does_not_mix_uncheckpointed_stream_result(
    storage: RolloutContextStore, carrier_saver: RolloutCheckpointSaver,
    stream_result_timing: str,
) -> None:
    """固定 checkpoint 的工具声明不得与稍后 active view 的唯一 stream 结果混读。"""
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    root = storage.storage.read_items(SESSION_ID, thread_id)[0]
    user = storage.storage._codec().from_dict(storage.storage._codec().project_message((root,)))
    stream_call = CanonicalItemRecord.create(
        item_sequence=1, item_id="item-live-call", semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "live-call", "invocation_id": MODEL_CALL_ID},
        payload={"tool_call_id": SCOPED_TOOL_CALL_ID, "name": "task", "args": {}},
        projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID),
        turn_id=TURN_ID, turn_scope=TurnScope.TURN_MEMBER, message_group_id="message-live-call",
        wire_role="assistant",
    )
    storage.executions.append_items(SESSION_ID, thread_id, (stream_call,), execution_id=MODEL_CALL_ID)
    assistant = AIMessage(
        content="", id="request-call",
        tool_calls=[{"id": PROVIDER_TOOL_CALL_ID, "name": "task", "args": {}}],
        response_metadata={"model_call_id": MODEL_CALL_ID},
    )
    admission = carrier_saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=MODEL_CALL_ID)
    owner = MutationIntentOwner(session_id=SESSION_ID, thread_id=thread_id)
    config = build_checkpoint_config(thread_id)

    def put(checkpoint_id: str, messages: list[object], parent_config):
        checkpoint = empty_checkpoint()
        checkpoint["id"] = checkpoint_id
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": checkpoint_id}
        checkpoint["updated_channels"] = ["messages"]
        with execution_admission_scope(admission, langgraph_thread_id=thread_id):
            return carrier_saver.put(parent_config, checkpoint, {"source": "unit"}, {"messages": checkpoint_id})

    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
        if stream_result_timing == "before_checkpoint":
            storage.executions.append_items(SESSION_ID, thread_id, (_live_tool_result(3),), execution_id=MODEL_CALL_ID)
        fixed = put("checkpoint-call-only", [user, assistant], config)
        if stream_result_timing == "after_checkpoint":
            storage.executions.append_items(SESSION_ID, thread_id, (_live_tool_result(3),), execution_id=MODEL_CALL_ID)
        intermediate = put("checkpoint-before-result-carrier", [user, assistant], fixed)
        # 私有 checkpoint 尚未提交结果；读取当前执行 checkpoint 必须等于按 ID 读取。
        explicit = carrier_saver.get_tuple(fixed)
        latest = carrier_saver.get_tuple(config)
        assert explicit is not None and latest is not None
        assert latest.checkpoint["id"] == intermediate["configurable"]["checkpoint_id"]
        assert latest.checkpoint["channel_values"]["messages"] == explicit.checkpoint["channel_values"]["messages"]
        assert [message.type for message in latest.checkpoint["channel_values"]["messages"]] == ["human", "ai"]
        assert _catalog_tool_result_ids(storage) == [_live_tool_result(3).item_id]
        assert carrier_saver.get_tuple(intermediate).checkpoint["channel_values"]["messages"] == explicit.checkpoint["channel_values"]["messages"]

        result = ToolMessage(
            content=RESULT_CONTENT, id="request-result", name="task", tool_call_id=PROVIDER_TOOL_CALL_ID,
            response_metadata={"model_call_id": MODEL_CALL_ID},
        )
        put("checkpoint-with-result", [user, assistant, result], intermediate)
        completed = carrier_saver.get_tuple(config)
        assert completed is not None
        messages = completed.checkpoint["channel_values"]["messages"]
        assert [message.type for message in messages] == ["human", "ai", "tool"]
        assert messages[-1].content == RESULT_CONTENT
        assert messages[-1].tool_call_id == PROVIDER_TOOL_CALL_ID
        assert carrier_saver.get_tuple(fixed).checkpoint == explicit.checkpoint

        fixed_id = fixed["configurable"]["checkpoint_id"]
        with sqlite3.connect(storage.storage.index_path(SESSION_ID)) as connection:
            selections = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT checkpoint.checkpoint_id, history.branch_id, messages.branch_id, "
                    "messages.view_id, messages.view_kind, history.control_sequence, messages.control_sequence "
                    "FROM checkpoints AS checkpoint "
                    "JOIN context_views AS history ON history.view_id = checkpoint.view_id "
                    "JOIN checkpoint_channels AS channel ON channel.checkpoint_id = checkpoint.checkpoint_id AND channel.channel_name = 'messages' "
                    "JOIN context_views AS messages ON messages.view_id = channel.context_view_id"
                )
            }
            previous = selections[fixed_id]
            future = selections[completed.checkpoint["id"]]
            assert previous[0] == previous[1] == future[0] == future[1]
            assert previous[3] == future[3] == "checkpoint_messages"
            assert previous[4] == previous[5] < future[4] == future[5]
            connection.execute(
                "UPDATE checkpoint_channels SET context_view_id = ? WHERE checkpoint_id = ? AND channel_name = 'messages'",
                (future[2], fixed_id),
            )
        # 同 branch 的未来结果 view 也不能成为旧执行快照的 messages channel。
        with pytest.raises(RuntimeError, match="messages selection 与冻结 checkpoint 不一致"):
            carrier_saver.get_tuple(fixed)
        assert carrier_saver.get_tuple(config).checkpoint == completed.checkpoint
        with sqlite3.connect(storage.storage.index_path(SESSION_ID)) as connection:
            assert connection.execute(
                "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id = ? AND channel_name = 'messages'",
                (fixed_id,),
            ).fetchone() == (future[2],)
            connection.execute(
                "UPDATE checkpoint_channels SET context_view_id = ? WHERE checkpoint_id = ? AND channel_name = 'messages'",
                (previous[2], fixed_id),
            )
        assert carrier_saver.get_tuple(fixed).checkpoint == explicit.checkpoint
