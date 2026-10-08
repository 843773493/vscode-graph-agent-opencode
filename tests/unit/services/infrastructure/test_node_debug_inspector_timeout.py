"""Node Inspector 命令超时的可诊断性单元测试。

直接构造 NodeDebugInspector 并注入永不回包的假 socket，验证命令超时不会外泄
asyncio 的空消息 TimeoutError（OSError），而必须抛出带方法名与超时值的
RuntimeError，让上层 API 与前端拿到可读原因。不依赖真实 Node 进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.support.node_debug_dependencies import TEST_THREAD_ID


class _SilentSocket:
    """只接受发送、永不回包的 Inspector socket 替身。"""

    async def send(self, message: str) -> None:
        return None


def _runtime() -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=Path("/tmp"),
        script_path=Path("/tmp/entry.mjs"),
        relative_script_path="entry.mjs",
        command_timeout_seconds=0.05,
    )
    runtime.inspector.socket = _SilentSocket()  # type: ignore[assignment]
    return runtime


def _runtime_at(tmp_path: Path) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=tmp_path,
        script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs",
        command_timeout_seconds=0.05,
    )
    return runtime


def _inspector() -> NodeDebugInspector:
    return NodeDebugInspector(
        workspace_root=Path("/tmp"),
        append_action=lambda *args, **kwargs: None,
        clear_stop_snapshot=lambda runtime: None,
    )


@pytest.mark.asyncio
async def test_command_timeout_raises_diagnosable_runtime_error() -> None:
    """命令超时必须带方法名与超时值，绝不外泄空消息的 OSError。"""
    runtime = _runtime()
    with pytest.raises(RuntimeError) as caught:
        await _inspector().command(runtime, "Debugger.evaluateOnCallFrame")
    message = str(caught.value)
    assert "Debugger.evaluateOnCallFrame" in message
    assert "超时" in message
    # 超时上报后不得残留待回包命令。
    assert runtime.inspector.pending_commands == {}


@pytest.mark.asyncio
async def test_send_failure_clears_pending_command_and_preserves_exception(
    tmp_path: Path,
) -> None:
    runtime = _runtime_at(tmp_path)
    original = OSError("synthetic send failure")

    class FailingSocket:
        async def send(self, message: str) -> None:
            del message
            raise original

    runtime.inspector.socket = FailingSocket()  # type: ignore[assignment]

    with pytest.raises(OSError) as caught:
        await _inspector().command(runtime, "Runtime.enable")

    assert caught.value is original
    assert runtime.inspector.pending_commands == {}


@pytest.mark.asyncio
async def test_cancelled_send_clears_pending_command(tmp_path: Path) -> None:
    runtime = _runtime_at(tmp_path)
    entered_send = asyncio.Event()

    class BlockedSocket:
        async def send(self, message: str) -> None:
            del message
            entered_send.set()
            await asyncio.Event().wait()

    runtime.inspector.socket = BlockedSocket()  # type: ignore[assignment]
    command = asyncio.create_task(
        _inspector().command(runtime, "Runtime.enable")
    )
    await asyncio.wait_for(entered_send.wait(), timeout=1)
    command.cancel()

    with pytest.raises(asyncio.CancelledError):
        await command

    assert runtime.inspector.pending_commands == {}


@pytest.mark.asyncio
async def test_frame_variable_timeout_raises_diagnosable_runtime_error() -> None:
    """局部变量读取超时同样必须可诊断。"""
    runtime = _runtime()

    async def _never_finishes() -> None:
        await asyncio.Event().wait()

    runtime.inspector.variable_hydration_task = asyncio.create_task(_never_finishes())
    try:
        with pytest.raises(RuntimeError) as caught:
            await _inspector().wait_for_frame_variables(runtime)
        assert "读取局部变量超时" in str(caught.value)
    finally:
        runtime.inspector.variable_hydration_task.cancel()
        await asyncio.gather(
            runtime.inspector.variable_hydration_task, return_exceptions=True
        )
