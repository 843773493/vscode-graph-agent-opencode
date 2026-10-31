"""调试工具共用失败事实，原生诊断不进入结果和动作正文。"""

from __future__ import annotations

import errno
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.debugging import create_debugging_tools
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414
)
from tests.support.diagnostic_failures import (
    sqlite_failure as sqlite_failure,  # noqa: PLC0414
)
from tests.unit.agents.tools.test_debugging_tools import (
    debug_service_factory as debug_service_factory,  # noqa: PLC0414
)


@pytest.fixture(params=["missing", "permission", "rename", "business", "timeout", "sqlite"])
def failure_case(request: pytest.FixtureRequest, sqlite_failure: sqlite3.OperationalError) -> tuple[Exception, str, str]:
    if request.param == "sqlite":
        return sqlite_failure, "DEBUG_TOOL_FAILED", "执行数据库失败: error=OperationalError, sqlite_errorcode=14"
    location = "/private/debug-tool-secret"
    if request.param == "missing":
        return (
            FileNotFoundError(errno.ENOENT, "调试文件不可用", location),
            "INVALID_DEBUG_ARGUMENT",
            "执行系统 I/O 失败: error=FileNotFoundError, errno=2",
        )
    if request.param == "permission":
        return (
            PermissionError(
                errno.EACCES, "调试文件不可读", r"C:\private\debug-tool-secret"
            ),
            "DEBUG_TOOL_FAILED",
            "执行系统 I/O 失败: error=PermissionError, errno=13",
        )
    if request.param == "rename":
        return (
            OSError(
                errno.EXDEV,
                "调试文件移动失败",
                location,
                None,
                r"D:\private\debug-tool-secret",
            ),
            "DEBUG_TOOL_FAILED",
            "执行系统 I/O 失败: error=OSError, errno=18",
        )
    if request.param == "timeout":
        return TimeoutError("调试命令等待超时"), "DEBUG_TOOL_FAILED", "调试命令等待超时"
    return (
        RuntimeError("没有活动的调试会话"),
        "NO_ACTIVE_DEBUG_SESSION",
        "没有活动的调试会话",
    )


@pytest.mark.parametrize(
    "tool_name", ["step_over", "add_breakpoint", "get_variables_values"]
)
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_debug_tool_failure_keeps_action_and_result_consistent(
    tmp_path: Path,
    debug_service_factory,
    diagnostic_sink: DiagnosticFailure,
    failure_case: tuple[Exception, str, str],
    tool_name: str,
    sink_fails: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original, code, message = failure_case
    if sink_fails:
        diagnostic_sink.primary = original
        message += ", diagnostic_sink_error=NotADirectoryError"
    session_id, thread_id = "ses_debug_failure", "thr_00000000700070008000000000000001"
    state = NodeDebugStateDTO(session_id=session_id, thread_id=thread_id, status="idle")
    service = debug_service_factory(tmp_path)
    service.get_state = AsyncMock(return_value=state)
    service.record_tool_action = AsyncMock(return_value=state)
    service.apply_action = AsyncMock(side_effect=original)
    service.get_variables = AsyncMock(side_effect=original)
    if tool_name == "add_breakpoint":
        service.workspace_relative_path.side_effect = original
    context = ToolInvocationContext(
        thread_binding=ThreadRuntimeBinding(session_id=session_id, thread_id=thread_id)
    )
    context.set_tool_call_id("call_debug_failure")
    (tool,) = [
        tool
        for tool in create_debugging_tools(
            session_id=session_id,
            node_debug_service=service,
            invocation_context=context,
        )
        if tool.name == tool_name
    ]
    arguments = {
        "step_over": {},
        "add_breakpoint": {"fileFullPath": "entry.mjs", "line": 1},
        "get_variables_values": {"variableNames": ["answer"]},
    }[tool_name]
    result = json.loads(await tool.ainvoke(arguments))
    assert result["ok"] is False and result["error"] == {
        "code": code,
        "message": message,
    }
    service.record_tool_action.assert_awaited_once_with(
        session_id=session_id,
        thread_id=thread_id,
        tool_name=tool_name,
        tool_call_id="call_debug_failure",
        extension_catalog_binding=None,
        result="error",
        message=message,
    )
    assert "debug-tool-secret" not in json.dumps(
        result
    ) and "diagnostic-log-secret" not in json.dumps(result) and "sqlite-execution-private" not in json.dumps(result)
    assert any(
        record.exc_info and record.exc_info[1] is original for record in caplog.records
    )
    if sink_fails:
        assert len(diagnostic_sink.errors) == 1
        assert any(
            str(diagnostic_sink.errors[0]) in note for note in original.__notes__
        )
