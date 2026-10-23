"""重启时沿原 stream 和原 tool attempt 收敛，不重发模型输出或重复结果。"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.domain.itemized.runtime import ModelCallRecord
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@dataclass(frozen=True, slots=True)
class _PendingStreamRecovery:
    runtime: MessageStreamRuntime
    message_stream_writer: MagicMock
    canonical_item_sink: AsyncMock
    tool_result_sink: AsyncMock
    model_call_registrar: AsyncMock
    assistant_message: AIMessage
    model_call: ModelCallRecord


@pytest.fixture
def pending_stream_recovery() -> _PendingStreamRecovery:
    model_call = ModelCallRecord(
        session_id="session",
        thread_id="thread",
        model_call_id="model",
        execution_id="execution",
        attempt=1,
        provider="fixture",
    )
    assistant_message = AIMessage(
        content="",
        id="carrier",
        tool_calls=[
            {
                "id": "pending",
                "name": "read_context",
                "args": {},
                "type": "tool_call",
            },
            {
                "id": "done",
                "name": "send_message_to_session",
                "args": {},
                "type": "tool_call",
            },
        ],
    )
    stream_snapshot_payload = {
        "stream_status": "open",
        "current_model_call_id": "model",
        "current_attempt": 1,
        "blocks": [],
        "tool_calls": [
            {
                "tool_call_id": f"model:tool-call:{provider_tool_call_id}",
                "status": "completed",
            }
            for provider_tool_call_id in ("pending", "done")
        ],
        "tool_executions": [
            {
                "tool_call_id": "model:tool-call:pending",
                "tool_execution_id": "original-pending",
                "tool_name": "read_context",
                "status": "running",
            },
            {
                "tool_call_id": "model:tool-call:done",
                "tool_execution_id": "original-done",
                "tool_name": "send_message_to_session",
                "status": "completed",
            },
        ],
    }
    message_stream_writer = MagicMock(spec=["snapshot", "commit"])
    message_stream_writer.snapshot = AsyncMock(
        return_value={"payload": stream_snapshot_payload}
    )
    message_stream_writer.commit = AsyncMock()
    canonical_item_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    model_call_registrar = AsyncMock(return_value="execution")
    runtime = MessageStreamRuntime(
        message_stream_writer,
        canonical_item_sink=canonical_item_sink,
        tool_result_sink=tool_result_sink,
        canonical_turn_id="turn",
        model_call_registrar=model_call_registrar,
    )
    return _PendingStreamRecovery(
        runtime=runtime,
        message_stream_writer=message_stream_writer,
        canonical_item_sink=canonical_item_sink,
        tool_result_sink=tool_result_sink,
        model_call_registrar=model_call_registrar,
        assistant_message=assistant_message,
        model_call=model_call,
    )


@pytest.mark.asyncio
async def test_pending_attempt_finishes_once_and_completed_batch_member_stays_terminal(
    pending_stream_recovery: _PendingStreamRecovery,
) -> None:
    recovery = pending_stream_recovery
    await recovery.runtime.restore_pending_model(
        recovery.assistant_message, recovery.model_call
    )
    recovery.model_call_registrar.assert_not_awaited()
    recovery.message_stream_writer.commit.assert_not_awaited()
    for provider_tool_call_id, tool_name in (
        ("done", "send_message_to_session"),
        ("pending", "read_context"),
    ):
        callback_run_id = f"new-callback-{provider_tool_call_id}"
        await recovery.runtime.wait_tool_start(
            "model", provider_tool_call_id, callback_run_id
        )
        tool_call_id = await recovery.runtime.tool_call_for_callback(callback_run_id)
        assert tool_call_id == f"model:tool-call:{provider_tool_call_id}"
        tool_attempt_id = recovery.runtime.resumed_tool_execution_id(
            tool_call_id, callback_run_id
        )
        assert tool_attempt_id == f"original-{provider_tool_call_id}"
        await recovery.runtime.start_tool(
            tool_execution_id=tool_attempt_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        assert (
            recovery.runtime.resumed_tool_execution_id(tool_call_id, callback_run_id)
            == tool_attempt_id
        )
        await recovery.runtime.complete_tool(
            tool_execution_id=tool_attempt_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="succeeded",
            result="冻结原结果",
        )
    assert [
        call.args[0] for call in recovery.message_stream_writer.commit.await_args_list
    ] == ["tool.completed"]
    assert all(
        call.args[1]["tool_execution_id"] == "original-pending"
        for call in recovery.message_stream_writer.commit.await_args_list
    )
    recovery.tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = recovery.tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model"
    assert completion.tool_call_id == "model:tool-call:pending"
    assert completion.tool_invocation_id == "tool-invocation:model:pending"
    assert completion.tool_attempt_id == "original-pending"
    assert completion.tool_name == "read_context"
    assert completion.content == "冻结原结果"
    assert completion.tool_outcome == "success"


@pytest.mark.asyncio
async def test_mismatched_stream_never_publishes_recovery_events(
    pending_stream_recovery: _PendingStreamRecovery,
) -> None:
    recovery = pending_stream_recovery
    recovery.message_stream_writer.snapshot.return_value["payload"][
        "current_model_call_id"
    ] = "other"
    with pytest.raises(RuntimeError, match="ModelCall/状态不一致"):
        await recovery.runtime.restore_pending_model(
            recovery.assistant_message, recovery.model_call
        )
    recovery.message_stream_writer.commit.assert_not_awaited()
    recovery.canonical_item_sink.assert_not_awaited()
    recovery.tool_result_sink.assert_not_awaited()
