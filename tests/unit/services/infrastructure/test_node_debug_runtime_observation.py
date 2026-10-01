"""Node Debug 运行时输出与进程监控链路的单元测试。

直接构造 NodeDebugRuntimeObserver，注入记录型假 command / 假进程与假回调，
精确打靶 monitor_process 的 exited/failed 终态、read_stream 的 Inspector 地址、
日志点输出/诊断归集与输出行数上限，以及 paused_at_breakpoint 判定矩阵。
不依赖真实 Node 进程。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugStackFrameDTO,
)
from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime


class _FakeStream:
    """按行返回预设字节，耗尽后返回空行结束读取。"""

    def __init__(self, lines: list[str], *, limit_error: bool = False) -> None:
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]
        #: 模拟 asyncio StreamReader 在单行超过缓冲上限时抛出的 ValueError。
        self._limit_error = limit_error

    async def readline(self) -> bytes:
        if self._limit_error:
            self._limit_error = False
            raise ValueError("Separator is found, but chunk is longer than limit")
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    def __init__(self, return_code: int, *, stdout: list[str] = (), stderr: list[str] = ()) -> None:
        self.stdout = _FakeStream(list(stdout))
        self.stderr = _FakeStream(list(stderr))
        self._return_code = return_code
        self.pid = 4242

    async def wait(self) -> int:
        return self._return_code


class _Recorder:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str, str]] = []
        self.claim_phases: list[tuple[str, str]] = []
        self.cleared = 0

    def append_action(self, runtime, action, message, *, actor="human", tool_name=None, tool_call_id=None, extension_catalog_binding=None, result="success"):
        self.actions.append((action, message, result))

    def mark_claim_phase(self, runtime, phase, reason=None):
        self.claim_phases.append((phase, reason or ""))

    def clear_stop_snapshot(self, runtime) -> None:
        self.cleared += 1


def _observer(
    recorder: _Recorder,
    *,
    max_output_lines: int = 100,
    max_stderr_lines: int = 50,
) -> NodeDebugRuntimeObserver:
    return NodeDebugRuntimeObserver(
        mark_claim_phase=recorder.mark_claim_phase,
        clear_stop_snapshot=recorder.clear_stop_snapshot,
        append_action=recorder.append_action,
        max_output_lines=max_output_lines,
        max_stderr_lines=max_stderr_lines,
    )


def _runtime(process: _FakeProcess | None = None) -> NodeDebugRuntime:
    return NodeDebugRuntime(
        session_id="ses_00000000000000000000000000000001",
        thread_id="main",
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=Path("/tmp"),
        script_path=Path("/tmp/entry.mjs"),
        relative_script_path="entry.mjs",
        process=process,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_monitor_process_marks_failed_and_reports_exit_code() -> None:
    """非零退出必须报 failed 且携带退出码；零退出才是 exited。"""
    recorder = _Recorder()
    observer = _observer(recorder)

    failed = _runtime(_FakeProcess(3))
    await observer.monitor_process(failed)
    assert failed.status == "failed"
    assert failed.error_message == "Node 调试进程退出，退出码: 3"
    assert recorder.claim_phases == [("settled", "进程已退出，退出码: 3")]
    assert recorder.cleared == 1

    ok = _runtime(_FakeProcess(0))
    await observer.monitor_process(ok)
    assert ok.status == "exited"
    assert ok.error_message is None


@pytest.mark.asyncio
async def test_monitor_process_prefers_logpoint_error_message() -> None:
    recorder = _Recorder()
    observer = _observer(recorder)
    runtime = _runtime(_FakeProcess(1))
    runtime.logpoint_error_message = "日志点求值失败: boom"
    await observer.monitor_process(runtime)
    assert runtime.status == "failed"
    assert runtime.error_message == "日志点求值失败: boom"


@pytest.mark.asyncio
async def test_monitor_process_skips_state_write_while_closing() -> None:
    """closing 期间只结清 claim，不再覆写终态（避免与停止路径互相覆盖）。"""
    recorder = _Recorder()
    observer = _observer(recorder)
    runtime = _runtime(_FakeProcess(0))
    runtime.closing = True
    runtime.status = "stopping"
    await observer.monitor_process(runtime)
    assert runtime.status == "stopping"
    assert recorder.claim_phases == [("settled", "进程已退出，退出码: 0")]


@pytest.mark.asyncio
async def test_read_stream_detects_inspector_url_and_logpoint_lines() -> None:
    recorder = _Recorder()
    observer = _observer(recorder)
    runtime = _runtime(
        _FakeProcess(
            0,
            stdout=[
                "Debugger listening on ws://127.0.0.1:9229/abc",
                "ordinary line",
                "__BOXTEAM_NODE_LOGPOINT__value=23",
                "__BOXTEAM_NODE_LOGPOINT_ERROR__\"ReferenceError: missing\"",
            ],
        )
    )
    await observer.read_stream(runtime, "stdout")
    assert runtime.inspector.inspector_url == "ws://127.0.0.1:9229/abc"
    assert runtime.inspector.inspector_ready.is_set()
    assert runtime.output == [
        "ordinary line",
        "[日志点] value=23",
        "[日志点错误] ReferenceError: missing",
    ]
    assert runtime.logpoint_error_message == "日志点求值失败: ReferenceError: missing"
    assert runtime.error_message == runtime.logpoint_error_message
    assert recorder.actions == [
        (
            "logpoint_error",
            "日志点求值失败: ReferenceError: missing",
            "error",
        )
    ]


@pytest.mark.asyncio
async def test_read_stream_ignores_stderr_logpoint_and_caps_output() -> None:
    recorder = _Recorder()
    observer = _observer(recorder, max_output_lines=2)
    runtime = _runtime(_FakeProcess(0, stdout=["a", "b", "c"]))
    await observer.read_stream(runtime, "stdout")
    assert runtime.output == ["b", "c"]

    stderr_runtime = _runtime(_FakeProcess(0, stderr=["__BOXTEAM_NODE_LOGPOINT__x=1"]))
    await observer.read_stream(stderr_runtime, "stderr")
    assert stderr_runtime.output == []
    assert stderr_runtime.logpoint_error_message is None


@pytest.mark.asyncio
async def test_read_stream_returns_without_process() -> None:
    observer = _observer(_Recorder())
    runtime = _runtime(None)
    await observer.read_stream(runtime, "stdout")
    assert runtime.output == []


@pytest.mark.asyncio
async def test_read_stream_buffers_stderr_and_flags_inspector_failure() -> None:
    """stderr 诊断行不得丢弃：端口占用必须被识别并唤醒握手失败分支。"""
    recorder = _Recorder()
    observer = _observer(recorder)
    runtime = _runtime(
        _FakeProcess(
            1,
            stderr=[
                "Starting inspector on 127.0.0.1:9229 failed: address already in use",
                "Debugger attached.",
            ],
        )
    )
    await observer.read_stream(runtime, "stderr")
    assert runtime.inspector.inspector_failed.is_set()
    assert runtime.inspector.inspector_failure_reason == "address already in use"
    assert runtime.stderr_lines == [
        "Starting inspector on 127.0.0.1:9229 failed: address already in use",
        "Debugger attached.",
    ]
    # stderr 只进诊断缓冲，不得混入程序 stdout 输出。
    assert runtime.output == []


@pytest.mark.asyncio
async def test_read_stream_caps_stderr_lines() -> None:
    observer = _observer(_Recorder(), max_stderr_lines=2)
    runtime = _runtime(_FakeProcess(0, stderr=["a", "b", "c"]))
    await observer.read_stream(runtime, "stderr")
    assert runtime.stderr_lines == ["b", "c"]


def test_terminal_error_message_never_empty_on_failure() -> None:
    """非零退出必须给出退出码与 stderr 尾行；零退出才是 None。"""
    observer = _observer(_Recorder())
    runtime = _runtime()
    runtime.stderr_lines = ["line-1", "line-2", "line-3", "line-4"]
    assert observer.terminal_error_message(runtime, 0) is None
    failed = observer.terminal_error_message(runtime, 3)
    assert failed == "Node 调试进程退出，退出码: 3；stderr: line-2 / line-3 / line-4"

    bare = _runtime()
    assert observer.terminal_error_message(bare, 3) == "Node 调试进程退出，退出码: 3"

    logpoint = _runtime()
    logpoint.logpoint_error_message = "日志点求值失败: boom"
    assert observer.terminal_error_message(logpoint, 1) == "日志点求值失败: boom"


def test_handshake_timeout_message_never_empty() -> None:
    observer = _observer(_Recorder())
    runtime = _runtime()
    assert observer.handshake_timeout_message(runtime, 3.0) == (
        "等待 Node Inspector 就绪超时（3 秒）"
    )
    runtime.stderr_lines = ["boom"]
    assert observer.handshake_timeout_message(runtime, 3.0) == (
        "等待 Node Inspector 就绪超时（3 秒）；stderr: boom"
    )


@pytest.mark.asyncio
async def test_read_stream_survives_oversized_line_with_visible_notice() -> None:
    """单行超限不得终止读取循环：必须显式标注截断并继续读取后续行。"""
    recorder = _Recorder()
    observer = _observer(recorder)
    process = _FakeProcess(0, stdout=["after-oversize"])
    process.stdout._limit_error = True
    runtime = _runtime(process)
    await observer.read_stream(runtime, "stdout")
    assert runtime.output[0].startswith("[输出截断] ")
    assert "stdout" in runtime.output[0]
    # 超限之后的行仍必须被读到，绝不静默丢掉后续输出。
    assert runtime.output[1] == "after-oversize"


def _breakpoint(breakpoint_id: str, *, path: str = "entry.mjs", line: int = 1, condition=None, log_message=None) -> NodeDebugBreakpointDTO:
    return NodeDebugBreakpointDTO(
        breakpoint_id=breakpoint_id,
        path=path,
        line=line,
        condition=condition,
        log_message=log_message,
        created_at=datetime.now(UTC),
    )


def test_paused_at_breakpoint_matrix() -> None:
    observer = _observer(_Recorder())
    frame = NodeDebugStackFrameDTO(
        call_frame_id="1", function_name="f", url="u", path="entry.mjs", line=1, column=1
    )
    plain = _breakpoint("node-bp-1")
    conditional = _breakpoint("node-bp-2", condition="x > 0")
    logpoint = _breakpoint("node-bp-3", log_message="x={x}")

    def _rt(breakpoints, *, paused=(), inspector=None, call_stack=()) -> NodeDebugRuntime:
        runtime = _runtime()
        runtime.breakpoints = {b.breakpoint_id: b for b in breakpoints}
        runtime.paused_breakpoint_ids = set(paused)
        runtime.inspector_breakpoint_ids = dict(inspector or {})
        runtime.call_stack = list(call_stack)
        return runtime

    # paused_breakpoint_ids 与已安装 Inspector 断点求交集。
    assert observer.paused_at_breakpoint(
        _rt([plain], paused=["i1"], inspector={"node-bp-1": "i1"})
    ) is True
    assert observer.paused_at_breakpoint(
        _rt([plain], paused=["i9"], inspector={"node-bp-1": "i1"})
    ) is False
    # 无 paused id 时回退按帧位置匹配，且忽略条件/logpoint 断点。
    assert observer.paused_at_breakpoint(_rt([plain], call_stack=[frame])) is True
    assert observer.paused_at_breakpoint(_rt([conditional], call_stack=[frame])) is False
    assert observer.paused_at_breakpoint(_rt([logpoint], call_stack=[frame])) is False
    assert observer.paused_at_breakpoint(
        _rt([_breakpoint("node-bp-4", path="other.mjs")], call_stack=[frame])
    ) is False
    assert observer.paused_at_breakpoint(_rt([plain])) is False
