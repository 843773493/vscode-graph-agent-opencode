"""重启时沿原 stream 和原 tool attempt 收敛，不重发模型输出或重复结果。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.domain.itemized.runtime import ModelCallRecord
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@pytest.fixture
def stream_recovery():
    model = ModelCallRecord("session", "thread", "model", "execution", 1, "fixture")
    assistant = AIMessage(content="", id="carrier", tool_calls=[
        {"id": "pending", "name": "read_context", "args": {}, "type": "tool_call"},
        {"id": "done", "name": "send_message_to_session", "args": {}, "type": "tool_call"},
    ])
    payload = {
        "stream_status": "open", "current_model_call_id": "model", "current_attempt": 1,
        "blocks": [], "tool_calls": [{"tool_call_id": f"model:tool-call:{name}", "status": "completed"} for name in ("pending", "done")],
        "tool_executions": [
            {"tool_call_id": "model:tool-call:pending", "tool_execution_id": "original-pending", "tool_name": "read_context", "status": "running"},
            {"tool_call_id": "model:tool-call:done", "tool_execution_id": "original-done", "tool_name": "send_message_to_session", "status": "completed"},
        ],
    }
    writer = MagicMock()
    writer.snapshot = AsyncMock(return_value={"payload": payload})
    writer.commit = AsyncMock()
    sink = AsyncMock()
    tool_result_sink = AsyncMock()
    registrar = AsyncMock(return_value="execution")
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=sink,
        tool_result_sink=tool_result_sink,
        canonical_turn_id="turn",
        model_call_registrar=registrar,
    )
    return runtime, writer, sink, tool_result_sink, registrar, assistant, model


@pytest.mark.asyncio
async def test_pending_attempt_finishes_once_and_completed_batch_member_stays_terminal(stream_recovery):
    runtime, writer, _sink, tool_result_sink, registrar, assistant, model = stream_recovery
    await runtime.restore_pending_model(assistant, model)
    registrar.assert_not_awaited()
    writer.commit.assert_not_awaited()
    for provider_id, name in (("done", "send_message_to_session"), ("pending", "read_context")):
        callback_run_id = f"new-callback-{provider_id}"
        await runtime.wait_tool_start("model", provider_id, callback_run_id)
        call = await runtime.tool_call_for_callback(callback_run_id)
        assert call == f"model:tool-call:{provider_id}"
        attempt = runtime.resumed_tool_execution_id(call, callback_run_id)
        assert attempt == f"original-{provider_id}"
        await runtime.start_tool(tool_execution_id=attempt, tool_call_id=call, tool_name=name)
        assert runtime.resumed_tool_execution_id(call, callback_run_id) == attempt
        await runtime.complete_tool(tool_execution_id=attempt, tool_call_id=call, tool_name=name, status="succeeded", result="冻结原结果")
    assert [call.args[0] for call in writer.commit.await_args_list] == ["tool.completed"]
    assert all(call.args[1]["tool_execution_id"] == "original-pending" for call in writer.commit.await_args_list)
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model"
    assert completion.tool_call_id == "model:tool-call:pending"
    assert completion.tool_invocation_id == "tool-invocation:model:pending"
    assert completion.tool_attempt_id == "original-pending"
    assert completion.tool_name == "read_context"
    assert completion.content == "冻结原结果"
    assert completion.tool_outcome == "success"


@pytest.mark.asyncio
async def test_mismatched_stream_never_publishes_recovery_events(stream_recovery):
    runtime, writer, sink, tool_result_sink, _, assistant, model = stream_recovery
    writer.snapshot.return_value["payload"]["current_model_call_id"] = "other"
    with pytest.raises(RuntimeError, match="ModelCall/状态不一致"):
        await runtime.restore_pending_model(assistant, model)
    writer.commit.assert_not_awaited()
    sink.assert_not_awaited()
    tool_result_sink.assert_not_awaited()
