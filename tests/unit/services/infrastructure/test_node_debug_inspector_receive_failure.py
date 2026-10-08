"""Node Inspector 接收循环故障的命令传播测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.support.node_debug_dependencies import TEST_THREAD_ID


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
        append_action=lambda *args, **kwargs: None,
        clear_stop_snapshot=lambda runtime: None,
    )


@pytest.mark.asyncio
async def test_receive_failure_reaches_pending_command(
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
) -> None:
    """接收循环故障必须将具体原因传给正在等待的 Inspector 命令。"""
    sent = asyncio.Event()

    class MalformedSocket:
        async def send(self, message: str) -> None:
            del message
            sent.set()

        def __aiter__(self) -> AsyncIterator[str]:
            async def messages() -> AsyncIterator[str]:
                yield "{"

            return messages()

    inspector_runtime.inspector.socket = MalformedSocket()  # type: ignore[assignment]
    command = asyncio.create_task(inspector.command(inspector_runtime, "Runtime.enable"))
    await asyncio.wait_for(sent.wait(), timeout=1)
    receiver = asyncio.create_task(inspector.receive_messages(inspector_runtime))

    with pytest.raises(RuntimeError, match="读取 Node Inspector 事件失败") as caught:
        await command
    await receiver

    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert "Expecting property name" in str(caught.value)
    assert inspector_runtime.status == "failed"
    assert inspector_runtime.error_message is not None
    assert "Expecting property name" in inspector_runtime.error_message
    assert inspector_runtime.inspector.pending_commands == {}
