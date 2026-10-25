"""Node Inspector 接收循环故障的命令传播测试。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

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
async def test_receive_failure_reaches_pending_command(
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    failure: str,
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
    with caplog.at_level(logging.ERROR, logger=NodeDebugInspector.__module__):
        receiver = asyncio.create_task(inspector.receive_messages(inspector_runtime))
        with pytest.raises(RuntimeError, match="读取 Node Inspector 事件失败") as caught:
            await command
        await receiver

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
