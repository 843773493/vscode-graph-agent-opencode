from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext

from app.core.failures import failure_detail
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.node_debug import NodeDebugBreakpointDTO
from app.services.infrastructure.node_debug.breakpoint.breakpoints import (
    BreakpointSource,
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
        _, digests = self._observe_sources([], {
            runtime.relative_script_path,
            *(breakpoint.path for breakpoint in runtime.breakpoints.values()),
        })
        return digests

    def _observe_sources(
        self,
        breakpoints: list[NodeDebugBreakpointDTO],
        loaded_paths: Iterable[str],
    ) -> tuple[list[NodeDebugBreakpointDTO], dict[str, str | None]]:
        # 同文件一次读盘、一次摘要；按文件处理，正文不跨文件或跨请求驻留。
        indices_by_path: dict[str, list[int]] = {}
        for index, breakpoint in enumerate(breakpoints):
            indices_by_path.setdefault(breakpoint.path, []).append(index)
        for path in loaded_paths:
            indices_by_path.setdefault(path, [])
        reconciled = list(breakpoints)
        digests: dict[str, str | None] = {}
        for path, indices in indices_by_path.items():
            source, uri = self._file_paths.resolve(path, allow_external=False)
            observed = BreakpointSource(source, uri=uri)
            digests[path] = observed.digest
            for index in indices:
                reconciled[index] = observed.reconcile(breakpoints[index])
            del observed
        return reconciled, digests

    async def reconcile(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        """调用方持有 Session 准入；源码 I/O 结算后再发布断点状态。"""
        # Session 独占准入串行化用户操作；运行态锁同时阻止 Inspector 改写断点。
        # 取消须等 worker 结束，才能释放两层保护。
        async with runtime.state_lock if runtime is not None else nullcontext():
            breakpoints = (
                list(runtime.breakpoints.values())
                if runtime is not None
                else self._session_state.pending_breakpoints((session_id, thread_id))
            )
            reconciled, digests = await run_sync_shielded(
                self._observe_sources, breakpoints,
                tuple(runtime.loaded_source_digests) if runtime is not None else (),
            )
        changed = False
        should_persist = False
        relocation_messages: list[str] = []
        invalidated_inspector_ids: list[tuple[str, str]] = []
        for breakpoint, next_breakpoint in zip(breakpoints, reconciled, strict=True):
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
                if digests[path] != loaded_digest:
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
                for breakpoint_id, inspector_id in invalidated_inspector_ids:
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
                            failure_detail(
                                error, stage="node-debug-breakpoint-invalidation",
                                label=(
                                    f"清理失效 Inspector 断点失败: session_id={session_id}, "
                                    f"thread_id={thread_id}, breakpoint_id={breakpoint_id}"
                                ),
                            ),
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
