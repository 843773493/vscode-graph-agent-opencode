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


class _SilentSocket:
    """只接受发送、永不回包的 Inspector socket 替身。"""

    async def send(self, message: str) -> None:
        return None


def _runtime() -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id="ses_00000000000000000000000000000001",
        thread_id="main",
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=Path("/tmp"),
        script_path=Path("/tmp/entry.mjs"),
        relative_script_path="entry.mjs",
        command_timeout_seconds=0.05,
    )
    runtime.inspector.socket = _SilentSocket()  # type: ignore[assignment]
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
