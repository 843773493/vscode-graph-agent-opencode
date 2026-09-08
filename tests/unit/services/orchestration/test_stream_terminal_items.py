"""实时终态到 canonical item 的状态与工具结果合同。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@pytest.fixture
def item_sink() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def runtime(item_sink: AsyncMock) -> MessageStreamRuntime:
    writer = MagicMock()
    writer.commit = AsyncMock()
    return MessageStreamRuntime(writer, canonical_item_sink=item_sink, canonical_turn_id="turn_1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "error", "expected"),
    [
        ("succeeded", "success", None, "success"),
        ("failed", "failure", "工具报错", "failure"),
        ("completed", "cancelled", None, "cancelled"),
        ("completed", "unknown", "用户中断时结果无法确认", "unknown"),
    ],
)
async def test_tool_result_separates_payload_status_from_execution_outcome(
    runtime: MessageStreamRuntime,
    item_sink: AsyncMock,
    status: str,
    outcome: str,
    error: str | None,
    expected: str,
) -> None:
    await runtime.start_model("model_1", "test")
    await runtime.start_tool(tool_execution_id="exec_1", tool_call_id="call_1", tool_name="shell")
    await runtime.complete_tool(
        tool_execution_id="exec_1",
        tool_call_id="call_1",
        tool_name="shell",
        status=status,
        outcome=outcome,
        error=error,
        result="工具结果",
    )

    item_sink.assert_awaited_once()
    item = item_sink.await_args.args[0][0]
    assert item.status == "completed"
    assert item.payload["tool_outcome"] == expected
    assert item.payload["result_id"] == "exec_1"
    assert item.metadata["execution_confirmed"] is (expected != "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "expected"), [("upstream_error", "failed"), ("user_interrupt", "partial")])
async def test_provider_failure_cannot_publish_completed_item(
    runtime: MessageStreamRuntime, item_sink: AsyncMock, outcome: str, expected: str
) -> None:
    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(content=[{"id": "text_1", "index": 0, "type": "text", "text": "尚未完成"}])
    )
    await runtime.fail_model(code="stopped", message="执行终止", outcome=outcome)

    item_sink.assert_awaited_once()
    item = item_sink.await_args.args[0][0]
    assert item.status == expected
    assert item.payload == "尚未完成"
    await runtime.finish_model()
    item_sink.assert_awaited_once()
