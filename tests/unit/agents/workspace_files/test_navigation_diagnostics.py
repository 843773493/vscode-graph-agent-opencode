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
from app.core.resource_uri import session_artifact_uri
from app.core.workspace_identity import read_workspace_id
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
    expected_message = (
        "执行系统 I/O 失败: error=PermissionError, errno=13, reason=目标拒绝读取, "
        "filename='/private/navigation-secret/guide.txt'"
        if failure == "filesystem" else "文件导航传输失败"
    )
    if log_fails:
        expected_message += ", diagnostic_sink_error=NotADirectoryError"
    assert detail == {
        "owner": "resource_file_operations", "code": "execution_failed", "reason": failure,
        "type": type(error).__name__, "errno": errno.EACCES if failure == "filesystem" else None,
        "message": expected_message,
    }
    assert ("navigation-secret" in message.content) is (failure == "filesystem")
    assert "diagnostic-log-secret" not in message.content
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


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("log_fails", [False, True])
async def test_owned_reader_without_errno_keeps_filesystem_failure_and_same_reference_retry(
    configure_filesystem_tools, tmp_path: Path, diagnostic_sink: DiagnosticFailure,
    synchronous: bool, log_fails: bool,
) -> None:
    from app.abstractions.resource_file_content import ResourceFileContent

    state = {"failing": True}
    error = PermissionError("原生reader拒绝 /private/owned-reader/secret")
    if log_fails:
        diagnostic_sink.primary = error

    def read(_uri: str, _maximum: int) -> ResourceFileContent:
        if state["failing"]:
            raise error
        return ResourceFileContent(b"original owned content\n", "text/plain")

    middleware = FilesystemMiddleware(backend=build_workspace_backend(tmp_path), tool_token_limit_before_evict=None)
    configure_filesystem_tools(middleware, workspace_root=tmp_path, artifact_reader=read)
    uri = session_artifact_uri(read_workspace_id(tmp_path), "ses_019b781159d47374805bc4427d081a14",
                               "tool-results", "tool_" + "a" * 24 + ".txt")
    tool = next(tool for tool in middleware.tools if tool.name == "read_file")
    call = {"name": "read_file", "args": {"path": uri, "runtime": SimpleNamespace(tool_call_id="owned-native")},
            "id": "owned-native", "type": "tool_call"}
    message = tool.invoke(call) if synchronous else await tool.ainvoke(call)
    assert message.status == "error" and message.tool_call_id == "owned-native"
    assert json.loads(message.content) == {
        "owner": "resource_file_operations", "code": "execution_failed", "reason": "filesystem",
        "type": "PermissionError", "errno": None,
        "message": "目标文件系统访问失败: stage=file-access, error=PermissionError, errno=None" + (
            ", diagnostic_sink_error=NotADirectoryError" if log_fails else ""),
    }
    if log_fails:
        record, = diagnostic_sink.records
        assert record.exc_info is not None and record.exc_info[1] is error
        assert len(diagnostic_sink.errors) == 1
        assert any(str(diagnostic_sink.errors[0]) in note for note in error.__notes__)
    state["failing"] = False
    recovered = tool.invoke(call) if synchronous else await tool.ainvoke(call)
    assert recovered.status == "success" and "original owned content" in recovered.content
