"""经 Gateway 插件传输的 Inspector 接收故障传播。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths


class BrokenPluginTransport:
    def __init__(self, error: OSError) -> None:
        self.error = error
        self.sent = asyncio.Event()

    async def send(self, _message: str) -> None:
        self.sent.set()

    async def messages(self) -> AsyncIterator[dict[str, object]]:
        raise self.error
        yield {}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_plugin_transport_io_error_reaches_pending_inspector_command(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    native = PermissionError(13, "Inspector transport read failed", "/target/private/source.mjs")
    transport = BrokenPluginTransport(native)
    runtime = NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=tmp_path,
        script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs",
    )
    runtime.inspector.socket = transport
    inspector = NodeDebugInspector(
        workspace_root=tmp_path,
        file_paths=node_debug_file_paths(tmp_path),
        append_action=lambda *_args, **_kwargs: None,
        clear_stop_snapshot=lambda _runtime: None,
    )

    command = asyncio.create_task(inspector.command(runtime, "Runtime.enable"))
    await asyncio.wait_for(transport.sent.wait(), timeout=1)
    receiver = asyncio.create_task(inspector.receive_messages(runtime))
    with pytest.raises(RuntimeError, match="读取 Node Inspector 事件失败") as caught:
        await command
    await receiver

    assert caught.value.__cause__ is native
    assert "PermissionError" in str(caught.value) and "errno=13" in str(caught.value)
    assert runtime.inspector.pending_commands == {}
    assert runtime.status == "failed"
    assert any(record.exc_info and record.exc_info[1] is native for record in caplog.records)
