from __future__ import annotations

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
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class NodeDebugSourceReconciliation:
    """按磁盘源码摘要对账运行时断点与源码变更状态。"""

    def __init__(
        self,
        *,
        file_paths: WorkspaceFilePaths,
        session_state: NodeDebugSessionState,
        command: NodeDebugCommandSender,
        append_action: NodeDebugActionAppender,
    ) -> None:
        self._file_paths = file_paths
        self._session_state = session_state
        self._command = command
        self._append_action = append_action

    def source_digests(self, runtime: NodeDebugRuntime) -> dict[str, str | None]:
        """读取该运行时脚本及全部断点源码的当前摘要。"""
        paths = {
            runtime.relative_script_path,
            *(breakpoint.path for breakpoint in runtime.breakpoints.values()),
        }
        digests = {}
        for path in paths:
            source, uri = self._file_paths.resolve(path, allow_external=False)
            digests[path] = source_digest(source, uri=uri)
        return digests

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
            source, uri = self._file_paths.resolve(breakpoint.path, allow_external=False)
            next_breakpoint = reconcile_breakpoint(
                breakpoint, source, uri=uri,
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
            changed_paths = set()
            for path, loaded_digest in runtime.loaded_source_digests.items():
                source, uri = self._file_paths.resolve(path, allow_external=False)
                if source_digest(source, uri=uri) != loaded_digest:
                    changed_paths.add(path)
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
