"""执行丢失的声明必须闭合协议，不能伪造工具 attempt 或覆盖已确认结果。"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.assembly.seal_preflight import (
    canonical_tool_pairings,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from tests.unit.services.infrastructure.rollout_context.test_tool_result_carrier_reuse import (
    MODEL_CALL_ID,
    PROVIDER_TOOL_CALL_ID,
    SESSION_ID,
    TURN_ID,
)
from tests.unit.services.infrastructure.rollout_context.test_tool_result_carrier_reuse import (
    carrier_saver as carrier_saver,  # noqa: PLC0414 - 同一真实 Saver 装具
)
from tests.unit.services.infrastructure.rollout_context.test_tool_result_carrier_reuse import (
    storage as storage,  # noqa: PLC0414 - 本文件隔离会话 bundle
)
from tests.unit.services.infrastructure.rollout_context.test_tool_result_carrier_reuse import (
    tool_result_owner as tool_result_owner,  # noqa: PLC0414 - 真实 prepare/ModelCall/声明
)


@pytest.mark.parametrize("existing_outcome", [None, "success", "failure", "cancelled", "unknown"])
def test_lost_execution_pairs_declaration_and_preserves_existing_result(
    storage, carrier_saver, tool_result_owner, existing_outcome,
):
    thread, completion = tool_result_owner
    previous = None
    if existing_outcome is not None:
        previous = carrier_saver.commit_tool_result(
            session_id=SESSION_ID, thread_id=thread, job_id="carrier-job",
            completion=replace(completion, tool_outcome=existing_outcome),
        )
    assert carrier_saver.mark_job_terminal_status(
        session_id=SESSION_ID, thread_id=thread, job_id="carrier-job", status="execution_lost",
    )
    results = [item for item in storage.storage.read_items(SESSION_ID, thread) if item.semantic_kind == "tool_result"]
    result, = results
    if previous is not None:
        assert result.to_dict() == previous.to_dict()
    else:
        assert result.projection_identity.model_call_id == MODEL_CALL_ID
        assert result.turn_id == TURN_ID and result.tool_call_id == PROVIDER_TOOL_CALL_ID
        assert result.producer_ref["producer_kind"] == "runtime"
        assert "tool_attempt_id" not in result.payload and "tool_invocation_id" not in result.payload
        assert result.payload["tool_outcome"] == "unknown" and result.payload["execution_confirmed"] is False
        with storage.storage.read_connection(SESSION_ID, thread) as connection:
            assert connection.execute(
                "SELECT result.commit_id=terminal.commit_id FROM item_catalog result "
                "JOIN storage_commits terminal ON terminal.commit_kind='terminal_convergence' "
                "WHERE result.item_id=?", (result.item_id,),
            ).fetchone() == (1,)
    before = storage.storage.jsonl_path(SESSION_ID, thread_id=thread).read_bytes()
    assert carrier_saver.mark_job_terminal_status(
        session_id=SESSION_ID, thread_id=thread, job_id="carrier-job", status="execution_lost",
    )
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread).read_bytes() == before


def test_explicit_execution_loss_reuses_original_notice_on_retry(storage, carrier_saver, tool_result_owner):
    thread, _completion = tool_result_owner
    first = carrier_saver.mark_execution_lost(
        SESSION_ID, thread_id=thread, turn_id=TURN_ID, execution_id=MODEL_CALL_ID, reason="process_exit",
    )
    before = storage.storage.jsonl_path(SESSION_ID, thread_id=thread).read_bytes()
    second = carrier_saver.mark_execution_lost(
        SESSION_ID, thread_id=thread, turn_id=TURN_ID, execution_id=MODEL_CALL_ID, reason="process_exit",
    )
    assert second["idempotent"] is True and second["commit_id"] == first["commit_id"]
    assert storage.storage.jsonl_path(SESSION_ID, thread_id=thread).read_bytes() == before


def test_loss_notice_and_terminal_state_roll_back_together(storage, carrier_saver, tool_result_owner, monkeypatch):
    thread, _completion = tool_result_owner
    original = storage.storage._commit_connection

    def fail_commit(connection):
        raise OSError("模拟终态提交失败")

    monkeypatch.setattr(storage.storage, "_commit_connection", fail_commit)
    with pytest.raises(OSError, match="模拟终态提交失败"):
        carrier_saver.mark_job_terminal_status(
            session_id=SESSION_ID, thread_id=thread, job_id="carrier-job", status="execution_lost",
        )
    monkeypatch.setattr(storage.storage, "_commit_connection", original)
    with storage.storage.read_connection(SESSION_ID, thread) as connection:
        assert connection.execute("SELECT outcome FROM executions WHERE execution_id=?", (MODEL_CALL_ID,)).fetchone() == ("unknown",)
        assert connection.execute("SELECT count(*) FROM item_catalog WHERE semantic_kind='tool_result'").fetchone() == (0,)
    assert carrier_saver.mark_job_terminal_status(
        session_id=SESSION_ID, thread_id=thread, job_id="carrier-job", status="execution_lost",
    )
    assert len([item for item in storage.storage.read_items(SESSION_ID, thread) if item.semantic_kind == "tool_result"]) == 1


@pytest.mark.parametrize("call_count", [2, 3])
def test_cold_followup_removes_entire_unfinished_checkpoint_tool_group(storage, carrier_saver, call_count):
    """真实 Saver 冷恢复后重写 channel，不得残留被移除消息的非 anchor 声明。"""
    thread = storage.storage.main_thread_id(SESSION_ID)
    root, = storage.storage.read_items(SESSION_ID, thread)
    codec = storage.storage._codec()
    user = codec.from_dict(codec.project_message((root,)))
    calls = [{"id": f"pending-{index}", "name": "task", "args": {}} for index in range(call_count)]
    for index, call in enumerate(calls):
        storage.executions.append_items(SESSION_ID, thread, (CanonicalItemRecord.create(
            item_sequence=1, item_id=f"stream-{index}", semantic_kind="tool_call",
            payload_kind="tool_call", status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": MODEL_CALL_ID, "invocation_id": MODEL_CALL_ID},
            payload={"name": call["name"], "args": call["args"], "tool_call_id": f"{MODEL_CALL_ID}:tool-call:{call['id']}"},
            projection_identity=ItemProjectionIdentity(model_call_id=MODEL_CALL_ID, block_id=f"stream-block-{index}", block_index=index),
            turn_id=TURN_ID, turn_scope="turn_member", message_group_id="stream-message", wire_role="assistant",
        ),), execution_id=MODEL_CALL_ID)
    owner = MutationIntentOwner(SESSION_ID, thread)
    config = cast(RunnableConfig, build_checkpoint_config(thread))
    checkpoint = empty_checkpoint()
    checkpoint.update(id="pending-checkpoint", channel_values={"messages": [user, AIMessage(
        content="", id="pending-message", tool_calls=calls, response_metadata={"model_call_id": MODEL_CALL_ID},
    )]}, channel_versions={"messages": "1"}, updated_channels=["messages"])
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread),
        execution_admission_scope(carrier_saver.execution_admission(
            SESSION_ID, thread_id=thread, execution_id=MODEL_CALL_ID,
        ), langgraph_thread_id=thread),
    ):
        fixed = carrier_saver.put(config, checkpoint, {"source": "loop"}, {"messages": "1"})
        frozen = carrier_saver.get_tuple(fixed).checkpoint
    assert carrier_saver.mark_job_terminal_status(
        session_id=SESSION_ID, thread_id=thread, job_id="carrier-job", status="execution_lost",
    )
    storage.executions.accept_turn(
        SESSION_ID, thread_id=thread, accepted_ingress_id="followup-ingress",
        acceptance_idempotency_key="followup-accept", job_id="followup-job",
        admission_idempotency_key="followup-admission", payload="硬重启后继续",
        turn_id="followup-turn", initial_execution_id="followup-execution",
    )
    with (
        RolloutCheckpointSaver(storage.storage.sessions_dir) as cold,
        session_thread_owner_scope(owner, langgraph_thread_id=thread),
        execution_admission_scope(cold.execution_admission(
            SESSION_ID, thread_id=thread, execution_id="followup-execution",
        ), langgraph_thread_id=thread),
    ):
        latest = cold.get_tuple(config)
        assert latest is not None
        messages = latest.checkpoint["channel_values"]["messages"]
        assert [message.type for message in messages] == ["human", *["ai"] * call_count, *["tool"] * call_count, "human"]
        assert [call["id"] for message in messages if isinstance(message, AIMessage)
                for call in message.tool_calls] == [call["id"] for call in calls]
        assert [result.tool_call_id for result in messages[1 + call_count:-1]] == [call["id"] for call in calls]
        cold.ensure_request_items(
            SESSION_ID, thread, turn_id="followup-turn", execution_id="followup-execution", messages=messages,
        )
        successor = latest.checkpoint.copy()
        successor.update(
            id="followup-checkpoint", channel_values={"messages": _DeltaSnapshot(messages)},
            channel_versions={"messages": "2"}, updated_channels=["messages"],
        )
        cold.put(latest.config, successor, {"source": "loop"}, {"messages": "2"})
        with cold._context_reader.open_snapshot(SESSION_ID, thread) as snapshot:
            selected = cold._storage.checkpoints.committed_context_items(
                snapshot, predecessor=cold._storage.assemblies.latest_context_assembly(snapshot),
            )
            assert not {item.item_id for item in selected if item.message_group_id == "message-pending-message"}
            pairs = canonical_tool_pairings({("canonical_item", item.item_id, thread): item for item in selected})
            assert len(pairs) == call_count
            projected = project_canonical_items(selected, preserve_order=True)
            assert [message.type for message in projected] == ["human", "ai", *["tool"] * call_count, "human"]
            assert isinstance(projected[1], AIMessage)
            assert [call["id"] for call in projected[1].tool_calls] == [call["id"] for call in calls]
        historical = cold.get_tuple(fixed)
        assert historical is not None and historical.checkpoint == frozen
