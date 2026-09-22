"""Node 调试运行时的输出与进程监控链路。

承载单个 Node 调试运行时的可观察行为：读取 stdout/stderr 行并归集输出、
识别日志点诊断与 Inspector 就绪地址，以及在进程终结时判定 exited/failed
终态并结清 launch claim。由 :class:`NodeDebugService` 构造并注入启动编排器。
"""

from __future__ import annotations

import re

from app.services.infrastructure.node_debug.breakpoint.breakpoint_expressions import (
    parse_logpoint_error,
    parse_logpoint_output,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugClaimPhaseMarker,
    NodeDebugRuntime,
    NodeDebugStopSnapshotClearer,
)

#: Inspector 在 stdout 上打印的就绪地址前缀。
INSPECTOR_URL_PATTERN = re.compile(r"Debugger listening on (ws://\S+)")

#: 单个运行时保留的输出行上限。
MAX_OUTPUT_LINES = 100


class NodeDebugRuntimeObserver:
    """读取调试进程输出并在进程终结时更新运行时终态。"""

    def __init__(
        self,
        *,
        mark_claim_phase: NodeDebugClaimPhaseMarker,
        clear_stop_snapshot: NodeDebugStopSnapshotClearer,
        append_action: NodeDebugActionAppender,
        max_output_lines: int = MAX_OUTPUT_LINES,
    ) -> None:
        self._mark_claim_phase = mark_claim_phase
        self._clear_stop_snapshot = clear_stop_snapshot
        self._append_action = append_action
        self._max_output_lines = max_output_lines

    async def read_stream(
        self,
        runtime: NodeDebugRuntime,
        stream_name: str,
    ) -> None:
        process = runtime.process
        if process is None:
            return
        stream = getattr(process, stream_name)
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            match = INSPECTOR_URL_PATTERN.search(text)
            if match:
                runtime.inspector.inspector_url = match.group(1)
                runtime.inspector.inspector_ready.set()
                continue
            if stream_name == "stdout" and text:
                logpoint_error = parse_logpoint_error(text)
                if logpoint_error is not None:
                    text = f"[日志点错误] {logpoint_error}"
                    async with runtime.state_lock:
                        runtime.logpoint_error_message = (
                            f"日志点求值失败: {logpoint_error}"
                        )
                        runtime.error_message = runtime.logpoint_error_message
                        self._append_action(
                            runtime,
                            "logpoint_error",
                            runtime.logpoint_error_message,
                            actor="system",
                            result="error",
                        )
                else:
                    logpoint_output = parse_logpoint_output(text)
                    if logpoint_output is not None:
                        text = f"[日志点] {logpoint_output}"
                async with runtime.state_lock:
                    runtime.output.append(text)
                    del runtime.output[: -self._max_output_lines]

    async def monitor_process(self, runtime: NodeDebugRuntime) -> None:
        process = runtime.process
        if process is None:
            return
        return_code = await process.wait()
        # 进程句柄已报告终态：这是可核实的终结，结清本实例的 claim。
        self._mark_claim_phase(
            runtime, "settled", f"进程已退出，退出码: {return_code}"
        )
        if runtime.closing:
            return
        async with runtime.state_lock:
            runtime.status = "exited" if return_code == 0 else "failed"
            self._clear_stop_snapshot(runtime)
            runtime.error_message = (
                runtime.logpoint_error_message
                if runtime.logpoint_error_message is not None
                else (
                    None
                    if return_code == 0
                    else f"Node 调试进程退出，退出码: {return_code}"
                )
            )

    @staticmethod
    def paused_at_breakpoint(runtime: NodeDebugRuntime) -> bool:
        if runtime.paused_breakpoint_ids:
            return bool(
                runtime.paused_breakpoint_ids
                & set(runtime.inspector_breakpoint_ids.values())
            )
        frame = runtime.call_stack[0] if runtime.call_stack else None
        if frame is None or frame.path is None:
            return False
        return any(
            breakpoint.log_message is None
            and breakpoint.condition is None
            and breakpoint.hit_condition is None
            and breakpoint.path == frame.path
            and frame.line in {breakpoint.line, breakpoint.actual_line}
            for breakpoint in runtime.breakpoints.values()
        )


__all__ = [
    "INSPECTOR_URL_PATTERN",
    "MAX_OUTPUT_LINES",
    "NodeDebugRuntimeObserver",
]
