"""Node Inspector 接收循环故障的命令传播测试。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import websockets

from app.core import failures
from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths


@pytest.fixture
def inspector_runtime(tmp_path: Path) -> NodeDebugRuntime:
    return NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=tmp_path,
        script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs",
    )


@pytest.fixture
def inspector(tmp_path: Path) -> NodeDebugInspector:
    return NodeDebugInspector(
        workspace_root=tmp_path,
        file_paths=node_debug_file_paths(tmp_path),
        append_action=lambda *args, **kwargs: None,
        clear_stop_snapshot=lambda runtime: None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "payload", "posix", "drive", "unc"])
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_receive_failure_reaches_pending_command(
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    failure: str,
    sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """公开故障保留目标和原因类型，原异常只进 cause 与诊断。"""
    sent = asyncio.Event()
    native_paths = {
        "posix": "/private/inspector-receive-secret",
        "drive": r"C:\private\inspector-receive-secret",
        "unc": r"\\server\private\inspector-receive-secret",
    }
    original = PermissionError(errno.EACCES, "读取原生位置失败", native_paths.get(failure))
    original.winerror = 5
    sink_errors: list[OSError] = []
    blocker = inspector_runtime.workspace_root / "receive-log-secret"
    blocker.write_text("日志父目录是普通文件")
    destination = blocker / "service.log"
    call_handlers = logging.Logger.callHandlers

    def emit(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if sink_fails and record.getMessage().startswith("读取 Node Inspector 事件失败:"):
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                sink_errors.append(error)
                raise

    monkeypatch.setattr(logging.Logger, "callHandlers", emit)

    class MalformedSocket:
        async def send(self, message: str) -> None:
            del message
            sent.set()

        def __aiter__(self) -> AsyncIterator[str]:
            async def messages() -> AsyncIterator[str]:
                if failure == "json":
                    yield '{"inspector-receive-secret":'
                elif failure == "payload":
                    yield json.dumps(["/private/inspector-receive-secret"])
                else:
                    raise original

            return messages()

    inspector_runtime.inspector.socket = MalformedSocket()  # type: ignore[assignment]
    command = asyncio.create_task(inspector.command(inspector_runtime, "Runtime.enable"))
    await asyncio.wait_for(sent.wait(), timeout=1)
    with caplog.at_level(logging.ERROR):
        receiver = asyncio.create_task(inspector.receive_messages(inspector_runtime))
        try:
            with pytest.raises(RuntimeError, match="读取 Node Inspector 事件失败") as caught:
                await command
        finally:
            await asyncio.gather(receiver, return_exceptions=True)
        assert receiver.exception() is None

    public = str(caught.value)
    cause = caught.value.__cause__
    assert inspector_runtime.session_id in public and inspector_runtime.thread_id in public
    assert "inspector-receive-secret" not in public
    if failure == "json":
        assert isinstance(cause, json.JSONDecodeError)
        assert "JSONDecodeError" in public
        assert f"line={cause.lineno}, column={cause.colno}, position={cause.pos}" in public
    elif failure == "payload":
        assert isinstance(cause, TypeError)
        assert "TypeError" in public and "inspector-receive-secret" in str(cause)
    else:
        assert cause is original
        assert "PermissionError" in public and "errno=13" in public and "winerror=5" in public
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert inspector_runtime.status == "failed"
    assert inspector_runtime.error_message == public
    assert inspector_runtime.inspector.pending_commands == {}
    if sink_fails:
        assert len(sink_errors) == 1 and sink_errors[0].errno == errno.ENOTDIR
        assert cause is not None
        assert any(str(sink_errors[0]) in note for note in cause.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in public
    else:
        assert not sink_errors and "diagnostic_sink_error" not in public
    assert str(destination) not in public and "receive-log-secret" not in public


@pytest.mark.parametrize("location", [
    "/private/inspector-command-secret", r"C:\private\inspector-command-secret",
    r"\\server\private\inspector-command-secret",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("phase", ["connect", "response", "result"])
@pytest.mark.parametrize("sink_fails", [False, True])
async def test_inspector_boundary_keeps_raw_failure_in_diagnostics_and_can_retry(
    inspector: NodeDebugInspector, inspector_runtime: NodeDebugRuntime,
    location: str, phase: str, sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    raw_message = f"Inspector 拒绝 {location}；" + "完整诊断" * 80
    native = RuntimeError(raw_message)

    class ReplySocket:
        async def send(self, message: str) -> None:
            command = json.loads(message)
            inspector_runtime.inspector.pending_commands[command["id"]].set_result(
                {"error": {"code": -32000, "message": raw_message}}
                if phase == "response" else {"result": [location]}
            )

    if phase == "connect":
        inspector_runtime.inspector.inspector_url = "ws://127.0.0.1:9229/unit"

        async def failed_connect(*args: object, **kwargs: object) -> None:
            raise native

        monkeypatch.setattr(websockets, "connect", failed_connect)
    else:
        inspector_runtime.inspector.socket = ReplySocket()  # type: ignore[assignment]

    if sink_fails:
        def failed_sink(*args: object, **kwargs: object) -> None:
            raise OSError("日志失败 /private/inspector-log-secret")

        monkeypatch.setattr(failures.logger, "error", failed_sink)

    with caplog.at_level(logging.ERROR), pytest.raises(
        TypeError if phase == "result" else RuntimeError,
    ) as caught:
        if phase == "connect":
            await inspector.connect(inspector_runtime)
        else:
            await inspector.command(inspector_runtime, "Runtime.enable")
    public = str(caught.value)
    assert inspector_runtime.session_id in public and inspector_runtime.thread_id in public
    assert "stage=node-debug-" in public
    assert location not in public and "inspector-command-secret" not in public
    assert "inspector-log-secret" not in public
    cause = caught.value.__cause__
    assert cause is not None
    if phase == "connect":
        assert cause is native
    elif phase == "result":
        assert repr([location]) in str(cause)
    else:
        assert str(cause) == raw_message
    if phase != "connect":
        assert "method=Runtime.enable" in public
    if phase == "response":
        assert "code=-32000" in public
    if sink_fails:
        assert "diagnostic_sink_error=OSError" in public
        assert any("inspector-log-secret" in note for note in cause.__notes__)
    else:
        assert str(cause) in caplog.text
        assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert inspector_runtime.inspector.pending_commands == {}
    # 不清空历史或重建runtime；原命令入口在故障解除后仍可用。
    if phase != "connect":
        class HealthySocket:
            async def send(self, message: str) -> None:
                command = json.loads(message)
                inspector_runtime.inspector.pending_commands[command["id"]].set_result(
                    {"result": {"ok": True}}
                )

        inspector_runtime.inspector.socket = HealthySocket()  # type: ignore[assignment]
        assert await inspector.command(inspector_runtime, "Runtime.enable") == {"ok": True}
        assert inspector_runtime.inspector.next_command_id == 3
