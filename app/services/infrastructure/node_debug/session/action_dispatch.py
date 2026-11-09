"""Node Debug 调试动作分发链路。

承载 apply_action 这一条产品入口的动作分发：owner 准入后按动作类型委托既有
breakpoint/evaluation/inspector/lifecycle 协作者，并与 start 共用 per-owner
临界区处理 stop。由 NodeDebugService 继承（宿主必须提供 _runtimes、_session_state、
_configuration_registry、_breakpoint_mutations、_evaluation、_inspector、_lifecycle、
_owner_key、_admit_mutation、_reconcile_session_sources、_assert_no_unsettled_claim、
_append_action 与已准入入口 _get_state_admitted），不反向依赖顶层 service.py。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRequest,
    NodeDebugClearBreakpointActionRequest,
    NodeDebugControlActionRequest,
    NodeDebugEvaluateActionRequest,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugStateDTO,
    NodeDebugUpdateBreakpointActionRequest,
)
from app.services.infrastructure.node_debug.runtime_state import publish_dap_event

if TYPE_CHECKING:
    from app.schemas.internal_v2.node_debug import NodeDebugBreakpointDTO
    from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
    from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class NodeDebugActionDispatchMixin:
    """调试动作分发链路的方法族（由 NodeDebugService 继承）。"""

    _workspace_root: Path
    _file_paths: WorkspaceFilePaths
    _get_state_admitted: Callable[[str, str], Awaitable[NodeDebugStateDTO]]

    async def apply_action(
        self,
        *,
        command: NodeDebugActionRequest,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        async with (
            self._admit_mutation(command.session_id, command.thread_id) as owner,
            self._owner_lock(owner),
        ):
            return await self._apply_action_admitted(
                owner, command=command, actor=actor,
                tool_name=tool_name, tool_call_id=tool_call_id,
            )

    async def _apply_action_admitted(
        self,
        owner: tuple[str, str],
        *,
        command: NodeDebugActionRequest,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        _expected_runtime: NodeDebugRuntime | None = None,
        _expected_pause_generation: int | None = None,
    ) -> NodeDebugStateDTO:
        """调用方已持有 Session 准入和 owner 锁；DAP 与工具共用唯一动作链。"""
        session_id, thread_id = owner
        self._session_state.ensure_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
        if _expected_runtime is not None and runtime is not _expected_runtime:
            raise RuntimeError("Node Debug run 已更换")
        if (_expected_pause_generation is not None and runtime is not None
                and runtime.pause_generation != _expected_pause_generation):
            raise ValueError("Node Debug 暂停代际已失效")
        breakpoint_mutation = isinstance(
            command,
            (
                NodeDebugSetBreakpointActionRequest,
                NodeDebugUpdateBreakpointActionRequest,
                NodeDebugClearBreakpointActionRequest,
            ),
        )
        previous_state = (
            await self._get_state_admitted(session_id, thread_id)
            if runtime is not None and breakpoint_mutation
            else None
        )
        if runtime is None and not isinstance(
            command,
            (
                NodeDebugSetBreakpointActionRequest,
                NodeDebugUpdateBreakpointActionRequest,
                NodeDebugClearBreakpointActionRequest,
            ),
        ):
            self._assert_no_unsettled_claim(
                owner,
                operation=f"调试动作 {command.action}",
            )
            raise RuntimeError(
                f"Node 调试会话不存在: session_id={session_id}, thread_id={thread_id}"
            )
        if isinstance(command, NodeDebugSetBreakpointActionRequest):
            self._configuration_registry.ensure_configuration_for_breakpoint(
                session_id,
                thread_id,
                path=command.params.path,
            )
            await self._breakpoint_mutations.set_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugUpdateBreakpointActionRequest):
            await self._breakpoint_mutations.update_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugClearBreakpointActionRequest):
            await self._breakpoint_mutations.clear_breakpoint(
                owner,
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugEvaluateActionRequest):
            await self._evaluation.evaluate(
                runtime,
                command.params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif isinstance(command, NodeDebugControlActionRequest):
            if command.action != "stop":
                await self._inspector.debugger_command(
                    runtime,
                    command.action,
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
                self._session_state.persist_runtime_state(
                    session_id, thread_id, runtime
                )
                return await self._get_state_admitted(session_id, thread_id)
            # stop 与 start 共用 owner 临界区，覆盖插件副作用与 claim worker 结算。
            outcome = await self._lifecycle.stop_runtime(runtime)
            if outcome != "reconcile_required":
                async with runtime.state_lock:
                    runtime.status = "exited"
                    runtime.error_message = None
                    self._append_action(
                        runtime,
                        "stop",
                        "已停止 Node Inspector",
                        actor=actor,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                    )
            if outcome == "reconcile_required":
                # 无法核实终态：保持 reconcile_required，不报告 stopped。
                self._session_state.persist_runtime_state(
                    session_id, thread_id, runtime
                )
                return await self._get_state_admitted(session_id, thread_id)
        self._session_state.persist_runtime_state(session_id, thread_id, runtime)
        result = await self._get_state_admitted(session_id, thread_id)
        if runtime is not None and previous_state is not None:
            self._publish_breakpoint_changes(
                runtime, previous_state.breakpoints, result.breakpoints,
            )
        return result


    def _publish_breakpoint_changes(
        self,
        runtime: NodeDebugRuntime,
        previous: list[NodeDebugBreakpointDTO],
        current: list[NodeDebugBreakpointDTO],
    ) -> None:
        previous_by_id = {item.breakpoint_id: item for item in previous}
        current_by_id = {item.breakpoint_id: item for item in current}
        for breakpoint_id in previous_by_id.keys() - current_by_id.keys():
            publish_dap_event(
                runtime,
                "breakpoint",
                {
                    "reason": "removed",
                    "breakpoint": self._dap_breakpoint(previous_by_id[breakpoint_id]),
                },
            )
        for breakpoint_id, breakpoint in current_by_id.items():
            if previous_by_id.get(breakpoint_id) != breakpoint:
                publish_dap_event(
                    runtime,
                    "breakpoint",
                    {"reason": "changed", "breakpoint": self._dap_breakpoint(breakpoint)},
                )

    def _dap_breakpoint(self, breakpoint: NodeDebugBreakpointDTO) -> dict[str, object]:
        source_path = self._file_paths.reference(
            self._workspace_root / Path(breakpoint.path)
        )
        return {
            "verified": breakpoint.verified,
            "line": breakpoint.actual_line or breakpoint.line,
            "source": {"name": Path(breakpoint.path).name, "path": source_path},
        }
