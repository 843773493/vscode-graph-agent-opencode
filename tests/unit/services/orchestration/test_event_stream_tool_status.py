"""工具结束事件的执行状态判定：显式失败不能被当成成功。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.agents.tool_result_text import tool_output_status
from app.services.orchestration.event_stream.tool_events import (
    activity_result_detail,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


# 调试扩展工具（app/agents/tools/debugging/payloads.py 的 _failure/_success）的固定结果形状：
# 以 ok 布尔值声明执行结论，不携带 status 字段。这里直接按该形状构造，避免
# 测试经由扩展工具装配链（ToolInvocationContext/MCP catalog）造成无关耦合。
def _debug_failure_json(code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}, "state": None},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tool_message(result_text: str, *, name: str = "start_debugging") -> ToolMessage:
    return ToolMessage(content=result_text, name=name, tool_call_id="call_1")


def test_declared_failure_via_ok_false_is_not_success() -> None:
    """调试扩展工具用 ok=false 声明失败；不能因缺少 status 字段被判成功。"""
    result_text = _debug_failure_json(
        "INVALID_DEBUG_ARGUMENT",
        "fileFullPath 不是有效的工作区相对路径",
    )
    assert json.loads(result_text)["ok"] is False

    message = _tool_message(result_text)
    assert tool_output_status(message) == "error"


def test_declared_success_via_ok_true_is_success() -> None:
    result_text = json.dumps(
        {"ok": True, "message": "done", "state": None},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert tool_output_status(_tool_message(result_text)) == "success"


@pytest.mark.parametrize("declared", ["error", "success"])
@pytest.mark.parametrize("ok", [False, True])
def test_declared_status_field_still_authoritative(declared: str, ok: bool) -> None:
    """status 字段声明的结果不得被 ok 字段覆盖。"""
    payload = {"status": declared, "ok": ok}
    result_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert tool_output_status(_tool_message(result_text)) == declared


def test_ok_false_marks_activity_phase_failed() -> None:
    """ok=false 的失败事实必须体现在 Activity detail 的 phase。"""
    result_text = _debug_failure_json("DEBUG_TOOL_FAILED", "adapter 崩溃")
    detail = activity_result_detail(
        result_text,
        tool_name="start_debugging",
        agent_id="default",
    )
    assert detail["phase"] == "failed"


def test_declared_status_error_marks_activity_phase_failed() -> None:
    """status=error 的既有声明仍必须标记 phase=failed。"""
    result_text = json.dumps(
        {"status": "error", "code": "tool_execution_timeout", "retryable": True},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    detail = activity_result_detail(
        result_text,
        tool_name="exec_command",
        agent_id="default",
    )
    assert detail["phase"] == "failed"
    assert detail["code"] == "tool_execution_timeout"
    assert detail["retryable"] is True


def test_plain_text_success_without_declaration_keeps_text_heuristic() -> None:
    """未声明状态时，保留原有文本启发式判定。"""
    class _StatuslessOutput:
        def __init__(self, content: str) -> None:
            self.content = content

    assert tool_output_status(_StatuslessOutput("命令输出正常")) == "success"
    assert tool_output_status(_StatuslessOutput("Error: 未找到文件")) == "error"


@pytest.fixture
async def tool_status_runtime():
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_declared_failure")
    sink = AsyncMock()
    runtime = MessageStreamRuntime(writer, canonical_item_sink=AsyncMock(), tool_result_sink=sink)
    await runtime.start_model("model_declared_failure", "primary")
    await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[{
        "index": 0, "id": "call_failure", "name": "apply_patch", "args": "{}",
    }]))
    await runtime.finish_model()
    call_id = runtime.claim_tool_call_id("model_declared_failure", "call_failure")
    await runtime.start_tool(tool_execution_id="attempt_failure", tool_call_id=call_id, tool_name="apply_patch")
    return runtime, sink


@pytest.mark.parametrize("content", ['{"status":"error","code":"capability_denied"}', '{"ok":false}'])
async def test_request_first_completion_preserves_declared_failure(tool_status_runtime, content):
    runtime, sink = tool_status_runtime
    await runtime.complete_tool_from_message(
        ToolMessage(content=content, tool_call_id="call_failure", name="apply_patch"),
        model_call_id="model_declared_failure",
    )
    completion, = sink.await_args.args
    assert completion.tool_outcome == "failure"
    assert completion.content == content
    assert completion.tool_call_id == "model_declared_failure:tool-call:call_failure"
