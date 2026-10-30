"""文件工具的日志次错不能替换原文件或传输失败。"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware

from app.agents.workspace_backend import build_workspace_backend
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 复用真实日志写入故障。
)


@pytest.fixture
def file_tools(configure_filesystem_tools, tmp_path: Path):
    source = tmp_path / "src/guide.txt"
    source.parent.mkdir()
    source.write_text("needle 原文件正文\n", encoding="utf-8")
    middleware = FilesystemMiddleware(backend=build_workspace_backend(tmp_path), tool_token_limit_before_evict=None)
    navigation = configure_filesystem_tools(middleware, workspace_root=tmp_path)
    return source, navigation, {tool.name: tool for tool in middleware.tools}


@pytest.mark.parametrize("name", ["ls", "read_file", "glob", "grep"])
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("failure", ["filesystem", "transport"])
@pytest.mark.parametrize("log_fails", [False, True])
async def test_navigation_rejection_survives_log_error_and_retries_same_tool(
    file_tools, diagnostic_sink: DiagnosticFailure, monkeypatch: pytest.MonkeyPatch,
    name: str, synchronous: bool, failure: str, log_fails: bool,
) -> None:
    source, navigation, tools = file_tools
    original = source.read_bytes()
    error = (
        PermissionError(errno.EACCES, "目标拒绝读取", "/private/navigation-secret/guide.txt")
        if failure == "filesystem" else httpx.ConnectError(
            "目标连接失败: /private/navigation-secret/socket",
            request=httpx.Request("GET", "http://private-host.invalid/native/navigation-secret"),
        )
    )
    if log_fails:
        diagnostic_sink.primary = error

    def reject(_value: str) -> str:
        raise error

    arguments = {"path": "src/guide.txt" if name == "read_file" else "src", "runtime": SimpleNamespace(tool_call_id="navigation-error")}
    if name in {"glob", "grep"}:
        arguments["pattern"] = "*.txt" if name == "glob" else "needle"
    call = {"name": name, "args": arguments, "id": "navigation-error", "type": "tool_call"}
    tool = tools[name]
    with monkeypatch.context() as patch:
        patch.setattr(navigation, "input_uri", reject)
        message = tool.invoke(call) if synchronous else await tool.ainvoke(call)
    assert message.status == "error" and message.tool_call_id == "navigation-error" and message.name == name
    detail = json.loads(message.content)
    expected_message = "文件导航文件系统访问失败" if failure == "filesystem" else "文件导航传输失败"
    if log_fails:
        expected_message += ", diagnostic_sink_error=NotADirectoryError"
    assert detail == {
        "owner": "resource_file_operations", "code": "execution_failed", "reason": failure,
        "type": type(error).__name__, "errno": errno.EACCES if failure == "filesystem" else None,
        "message": expected_message,
    }
    assert "navigation-secret" not in message.content and "diagnostic-log-secret" not in message.content
    assert source.read_bytes() == original
    if log_fails:
        sink_error, = diagnostic_sink.errors
        record, = diagnostic_sink.records
        assert sink_error.errno == errno.ENOTDIR
        assert record.exc_info is not None and record.exc_info[1] is error
        assert any(str(sink_error) in note for note in error.__notes__)
    else:
        assert not diagnostic_sink.errors and not getattr(error, "__notes__", [])
    recovered = tool.invoke(call) if synchronous else await tool.ainvoke(call)
    assert recovered.status == "success", recovered.content
    assert source.read_bytes() == original
