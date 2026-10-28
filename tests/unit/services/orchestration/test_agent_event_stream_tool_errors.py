"""SDK错误callback关闭原展示活动，真实ToolMessage独占结果正文。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.core.job_context import get_interruptible_phase
from app.services.orchestration.event_stream.processor import process_agent_event_stream
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.unit.services.orchestration.test_agent_event_stream_parallel_tools import (
    FakeAgent,
    FakeSessionChangesService,
)
from tests.unit.services.orchestration.test_agent_event_stream_parallel_tools import (
    workspace_root as workspace_root,  # noqa: PLC0414 - 复用工作区fixture
)


@pytest.mark.parametrize("tool_name,metadata,kind", [
    ("task", {}, "subagent.run"),
    ("terminal_exec", {"resource_activity": {"resource_id": "terminal-test"}}, "resource.operation"),
])
async def test_tool_error_closes_activity_without_fabricating_a_result(
    workspace_root: Path, tool_name: str, metadata: dict[str, object], kind: str,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_tool_error")
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_error", "primary")
    await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[{
        "index": 0, "id": "call_invalid", "name": tool_name, "args": '{"invalid":true}',
    }]))
    events = [
        {"event": "on_tool_start", "run_id": "run_invalid", "name": tool_name,
         "data": {"input": {"invalid": True}}, "metadata": metadata},
        {"event": "on_tool_error", "run_id": "run_invalid", "name": tool_name,
         "data": {"error": ValueError("SDK原始校验错误")}, "metadata": metadata},
    ]

    async def publish(*_args, **_kwargs):
        return None

    result = await process_agent_event_stream(
        agent=FakeAgent(events, tool_invocations={"run_invalid": ("model_error", "call_invalid")}),
        input_payload={"messages": []}, config={}, session_id="ses_error", thread_id="thr_error",
        job_id="job_error", turn_id="turn_error", agent_id="default", custom_tool_skill_sources={},
        publish=publish, session_changes_service=FakeSessionChangesService(), workspace_root=workspace_root,
        message_stream_runtime=runtime,
    )
    assert result.successful_tool_calls == ()
    assert get_interruptible_phase() == "text"
    activities = [call.args for call in writer.commit.await_args_list if call.args[0].startswith("activity.")]
    assert [entry[0] for entry in activities] == ["activity.started", "activity.updated", "activity.failed"]
    assert {entry[1]["kind"] for entry in activities} == {kind}
    assert len({entry[1]["activity_id"] for entry in activities}) == 1
    assert activities[-1][1]["summary"] == "SDK原始校验错误"
    assert not [call for call in writer.commit.await_args_list if call.args[0] == "tool.completed"]

    # 框架处理异常后的配对消息才是权威正文，不能被早到callback异常字符串替换。
    message = ToolMessage(content="框架返回的完整错误（含参数与校验提示）", status="error", tool_call_id="call_invalid", name=tool_name)
    await runtime.complete_tool_from_message(message, model_call_id="model_error")
    await runtime.complete_tool_from_message(message, model_call_id="model_error")
    results = [call.args[1] for call in writer.commit.await_args_list if call.args[0] == "tool.completed"]
    assert len(results) == 1 and results[0]["result"] == message.content
    assert results[0]["tool_execution_id"] == "run_invalid" and results[0]["status"] == "failed"
