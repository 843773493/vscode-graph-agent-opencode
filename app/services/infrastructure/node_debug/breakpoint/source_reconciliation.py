from __future__ import annotations

from pathlib import Path

from app.core.path_utils import safe_join
from app.schemas.internal_v2.node_debug import NodeDebugBreakpointDTO
from app.services.infrastructure.node_debug.breakpoint.breakpoints import (
    reconcile_breakpoint,
    source_digest,
)
from app.services.infrastructure.node_debug.runtime_state import (
    LIVE_NODE_DEBUG_RUNTIME_STATUSES,
    NodeDebugActionAppender,
    NodeDebugCommandSender,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)


class NodeDebugSourceReconciliation:
    """按磁盘源码摘要对账运行时断点与源码变更状态。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        session_state: NodeDebugSessionState,
        command: NodeDebugCommandSender,
        append_action: NodeDebugActionAppender,
    ) -> None:
        self._workspace_root = workspace_root
        self._session_state = session_state
        self._command = command
        self._append_action = append_action

    def source_digests(self, runtime: NodeDebugRuntime) -> dict[str, str | None]:
        """读取该运行时脚本及全部断点源码的当前摘要。"""
        paths = {
            runtime.relative_script_path,
            *(breakpoint.path for breakpoint in runtime.breakpoints.values()),
        }
        return {
            path: source_digest(safe_join(runtime.workspace_root, path))
            for path in paths
        }

    async def reconcile(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        """把断点锚点、源码变更标记和待安装断点与磁盘源码对齐。"""
        breakpoints = (
            list(runtime.breakpoints.values())
            if runtime is not None
            else self._session_state.pending_breakpoints((session_id, thread_id))
        )
        reconciled: list[NodeDebugBreakpointDTO] = []
        changed = False
        should_persist = False
        relocation_messages: list[str] = []
        invalidated_inspector_ids: list[tuple[str, str]] = []
        for breakpoint in breakpoints:
            next_breakpoint = reconcile_breakpoint(
                breakpoint,
                safe_join(self._workspace_root, breakpoint.path),
            )
            reconciled.append(next_breakpoint)
            if next_breakpoint != breakpoint:
                changed = True
                should_persist = True
                relocation_messages.append(
                    next_breakpoint.relocation_message
                    or f"断点状态已更新: {next_breakpoint.path}:{next_breakpoint.line}"
                )
            if runtime is not None and next_breakpoint.relocation_status != "current":
                inspector_id = runtime.inspector_breakpoint_ids.get(
                    breakpoint.breakpoint_id
                )
                if inspector_id is not None:
                    invalidated_inspector_ids.append(
                        (breakpoint.breakpoint_id, inspector_id)
                    )

        if runtime is not None:
            active = runtime.status in LIVE_NODE_DEBUG_RUNTIME_STATUSES
            changed_paths = {
                path
                for path, loaded_digest in runtime.loaded_source_digests.items()
                if source_digest(safe_join(runtime.workspace_root, path))
                != loaded_digest
            }
            if active and changed_paths:
                newly_changed_paths = changed_paths - runtime.source_changed_paths
                runtime.requires_restart = True
                runtime.source_changed_paths.update(changed_paths)
                if newly_changed_paths:
                    should_persist = True
                    self._append_action(
                        runtime,
                        "source_changed",
                        "磁盘源码已变化，相关断点已失效；如需运行新源码可重启调试: "
                        + "、".join(sorted(newly_changed_paths)),
                        actor="system",
                    )
            if changed:
                async with runtime.state_lock:
                    runtime.breakpoints = {
                        breakpoint.breakpoint_id: breakpoint
                        for breakpoint in reconciled
                    }
                    for breakpoint_id, _inspector_id in invalidated_inspector_ids:
                        runtime.inspector_breakpoint_ids.pop(breakpoint_id, None)
                for message in relocation_messages:
                    self._append_action(
                        runtime,
                        "breakpoint_reconciled",
                        message,
                        actor="system",
                    )
            if invalidated_inspector_ids and runtime.inspector.socket is not None:
                for _breakpoint_id, inspector_id in invalidated_inspector_ids:
                    try:
                        await self._command(
                            runtime,
                            "Debugger.removeBreakpoint",
                            {"breakpointId": inspector_id},
                        )
                    except Exception as error:  # noqa: BLE001 - 失效标记不能阻断调试
                        self._append_action(
                            runtime,
                            "breakpoint_invalidation_failed",
                            f"清理失效 Inspector 断点失败: {error}",
                            actor="system",
                            result="error",
                        )
        elif changed:
            self._session_state.set_pending_breakpoints(
                (session_id, thread_id), reconciled
            )
            for message in relocation_messages:
                self._session_state.append_pending_action(
                    session_id,
                    thread_id,
                    "breakpoint_reconciled",
                    message,
                    actor="system",
                )

        if should_persist:
            self._session_state.persist_runtime_state(session_id, thread_id, runtime)
