"""汇总原 Debug 插件 run 的输出与进程终态。"""

from __future__ import annotations

import logging

from app.services.infrastructure.node_debug.breakpoint.breakpoint_expressions import (
    parse_logpoint_error,
    parse_logpoint_output,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugClaimPhaseMarker,
    NodeDebugRuntime,
    NodeDebugStopSnapshotClearer,
    publish_dap_event,
)

logger = logging.getLogger(__name__)
MAX_OUTPUT_LINES = 100


class NodeDebugRuntimeObserver:
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

    async def monitor_process(self, runtime: NodeDebugRuntime) -> None:
        plugin = runtime.plugin_runtime
        if plugin is None:
            raise RuntimeError("Node Debug run 缺少原 Debug 插件连接")
        async for frame in plugin.events():
            event = frame.get("event")
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            if event in {"stdout", "stderr"} and isinstance(event, str):
                text = payload.get("text")
                if isinstance(text, str) and text:
                    self._record_output(runtime, event, text)
                continue
            if event != "process.exited":
                continue
            exit_code = payload.get("exit_code")
            runtime.target_exit_code = exit_code if type(exit_code) is int else None
            runtime.target_exited = True
            plugin.exit_code = runtime.target_exit_code
            await self._mark_claim_phase(runtime, "settled", "Debug 插件已确认目标进程退出")
            async with runtime.state_lock:
                runtime.status = "exited" if runtime.target_exit_code == 0 else "failed"
                runtime.error_message = self.terminal_error_message(
                    runtime, runtime.target_exit_code if runtime.target_exit_code is not None else -1,
                )
                self._clear_stop_snapshot(runtime)
            publish_dap_event(runtime, "terminated", {})
            return
        if not runtime.closing and runtime.status not in {"exited", "failed"}:
            async with runtime.state_lock:
                runtime.status = "execution_lost"
                runtime.error_message = "Backend 与原 Debug 插件 run 的控制通道已断开；目标状态未知"
            await self._mark_claim_phase(runtime, "execution_lost", runtime.error_message)
            publish_dap_event(runtime, "terminated", {})

    def _record_output(self, runtime: NodeDebugRuntime, stream: str, raw_text: str) -> None:
        for line in raw_text.rstrip().splitlines() or [raw_text.rstrip()]:
            text = line
            if stream == "stdout":
                logpoint_error = parse_logpoint_error(text)
                if logpoint_error is not None:
                    text = f"[日志点错误] {logpoint_error}"
                    runtime.logpoint_error_message = f"日志点求值失败: {logpoint_error}"
                    runtime.error_message = runtime.logpoint_error_message
                    self._append_action(
                        runtime, "logpoint_error", runtime.logpoint_error_message,
                        actor="system", result="error",
                    )
                else:
                    logpoint_output = parse_logpoint_output(text)
                    if logpoint_output is not None:
                        text = f"[日志点] {logpoint_output}"
            if text:
                runtime.output.append(text)
                del runtime.output[:-self._max_output_lines]
            publish_dap_event(runtime, "output", {
                "category": "stderr" if stream == "stderr" else "stdout",
                "output": text + "\n",
            })
            if stream == "stderr" and text:
                logger.warning(
                    "Node stderr: session_id=%s, thread_id=%s, pid=%s; %s",
                    runtime.session_id, runtime.thread_id, runtime.target_pid, text,
                )

    @staticmethod
    def terminal_error_message(runtime: NodeDebugRuntime, return_code: int) -> str | None:
        if runtime.logpoint_error_message is not None:
            return runtime.logpoint_error_message
        if return_code == 0:
            return None
        return f"Node 调试进程退出，退出码: {return_code}"

    @staticmethod
    def paused_at_breakpoint(runtime: NodeDebugRuntime) -> bool:
        if runtime.paused_breakpoint_ids:
            return bool(runtime.paused_breakpoint_ids & set(runtime.inspector_breakpoint_ids.values()))
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
