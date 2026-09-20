from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Literal

from app.core.path_utils import safe_join
from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugClearBreakpointParams,
    NodeDebugSetBreakpointParams,
    NodeDebugUpdateBreakpointParams,
)
from app.services.infrastructure.node_debug.breakpoint_expressions import (
    inspector_breakpoint_condition,
)
from app.services.infrastructure.node_debug.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugCommandSender,
    NodeDebugPendingActionAppender,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.thread_owner import NodeDebugOwner


class NodeDebugBreakpointMutations:
    """编排源码断点的 pending、运行时 mutation 和 Inspector 安装。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        configuration_factory: NodeDebugConfigurationFactory,
        pending_breakpoints: MutableMapping[
            NodeDebugOwner, list[NodeDebugBreakpointDTO]
        ],
        command: NodeDebugCommandSender,
        append_action: NodeDebugActionAppender,
        append_pending_action: NodeDebugPendingActionAppender,
    ) -> None:
        self._workspace_root = workspace_root
        self._configuration_factory = configuration_factory
        self._pending_breakpoints = pending_breakpoints
        self._command = command
        self._append_action = append_action
        self._append_pending_action = append_pending_action

    async def set_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: NodeDebugRuntime | None,
        params: NodeDebugSetBreakpointParams,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        raw_path = params.path
        line = params.line
        column = params.column
        condition = params.condition
        hit_condition = params.hit_condition
        log_message = params.log_message
        breakpoint = self._configuration_factory.create_breakpoint(
            path=raw_path,
            line=line,
            column=column,
            condition=condition,
            hit_condition=hit_condition,
            log_message=log_message,
        )
        script_path = safe_join(self._workspace_root, breakpoint.path)
        if runtime is None:
            session_id, thread_id = owner
            pending = self._pending_breakpoints.setdefault(owner, [])
            if self._matching_breakpoint(pending, breakpoint) is not None:
                raise ValueError(f"源码断点已存在: {breakpoint.path}:{line}:{column}")
            pending.append(breakpoint)
            self._append_pending_action(
                session_id,
                thread_id,
                "set_breakpoint",
                f"已设置源码断点 {breakpoint.path}:{line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            return
        async with runtime.state_lock:
            if self._matching_breakpoint(runtime.breakpoints.values(), breakpoint) is not None:
                raise ValueError(f"源码断点已存在: {breakpoint.path}:{line}:{column}")
            runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint
        if runtime.inspector.socket is not None and runtime.status in {"running", "paused"}:
            await self.install_breakpoint(runtime, breakpoint, script_path=script_path)
        async with runtime.state_lock:
            self._append_action(
                runtime,
                "set_breakpoint",
                f"已设置源码断点 {breakpoint.path}:{line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def update_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: NodeDebugRuntime | None,
        params: NodeDebugUpdateBreakpointParams,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        breakpoint_id = params.breakpoint_id
        session_id, thread_id = owner
        breakpoints: Iterable[NodeDebugBreakpointDTO] = (
            runtime.breakpoints.values()
            if runtime is not None
            else self._pending_breakpoints.get(owner, [])
        )
        current = next(
            (
                breakpoint
                for breakpoint in breakpoints
                if breakpoint.breakpoint_id == breakpoint_id
            ),
            None,
        )
        if current is None:
            raise ValueError(f"源码断点不存在: {breakpoint_id}")

        raw_path = params.path if "path" in params.model_fields_set else current.path
        line = params.line if "line" in params.model_fields_set else current.line
        column = (
            params.column if "column" in params.model_fields_set else current.column
        )
        condition = (
            params.condition
            if "condition" in params.model_fields_set
            else current.condition
        )
        hit_condition = (
            params.hit_condition
            if "hit_condition" in params.model_fields_set
            else current.hit_condition
        )
        log_message = (
            params.log_message
            if "log_message" in params.model_fields_set
            else current.log_message
        )
        if raw_path is None or line is None or column is None:
            raise ValueError("编辑源码断点的 path、line、column 不能为 null")
        updated = self._configuration_factory.create_breakpoint(
            path=raw_path,
            line=line,
            column=column,
            condition=condition,
            hit_condition=hit_condition,
            log_message=log_message,
        ).model_copy(
            update={
                "breakpoint_id": current.breakpoint_id,
                "created_at": current.created_at,
            }
        )
        conflict = self._matching_breakpoint(
            (
                breakpoint
                for breakpoint in breakpoints
                if breakpoint.breakpoint_id != breakpoint_id
            ),
            updated,
        )
        if conflict is not None:
            raise ValueError(f"源码断点位置已被占用: {updated.path}:{line}:{column}")

        if runtime is None:
            pending = self._pending_breakpoints.get(owner, [])
            pending[pending.index(current)] = updated
            self._append_pending_action(
                session_id,
                thread_id,
                "update_breakpoint",
                f"已更新源码断点 {updated.path}:{updated.line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            return

        previous_inspector_id = runtime.inspector_breakpoint_ids.get(breakpoint_id)
        if runtime.inspector.socket is not None and runtime.status in {"running", "paused"}:
            await self.install_breakpoint(
                runtime,
                updated,
                script_path=safe_join(self._workspace_root, updated.path),
            )
            if previous_inspector_id is not None:
                await self._command(
                    runtime,
                    "Debugger.removeBreakpoint",
                    {"breakpointId": previous_inspector_id},
                )
        else:
            async with runtime.state_lock:
                runtime.breakpoints[breakpoint_id] = updated
                runtime.inspector_breakpoint_ids.pop(breakpoint_id, None)
        async with runtime.state_lock:
            self._append_action(
                runtime,
                "update_breakpoint",
                f"已更新源码断点 {updated.path}:{updated.line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def clear_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: NodeDebugRuntime | None,
        params: NodeDebugClearBreakpointParams,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        breakpoint_id = params.breakpoint_id
        if runtime is None:
            session_id, thread_id = owner
            pending = self._pending_breakpoints.get(owner, [])
            for index, breakpoint in enumerate(pending):
                if breakpoint.breakpoint_id == breakpoint_id:
                    pending.pop(index)
                    if not pending:
                        self._pending_breakpoints.pop(owner, None)
                    self._append_pending_action(
                        session_id,
                        thread_id,
                        "clear_breakpoint",
                        f"已清除源码断点 {breakpoint.path}:{breakpoint.line}",
                        actor=actor,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                    )
                    return
            raise ValueError(f"源码断点不存在: {breakpoint_id}")
        inspector_id = runtime.inspector_breakpoint_ids.get(breakpoint_id)
        if inspector_id and runtime.inspector.socket is not None:
            await self._command(
                runtime,
                "Debugger.removeBreakpoint",
                {"breakpointId": inspector_id},
            )
        async with runtime.state_lock:
            if breakpoint_id not in runtime.breakpoints:
                raise ValueError(f"源码断点不存在: {breakpoint_id}")
            breakpoint = runtime.breakpoints.pop(breakpoint_id)
            runtime.inspector_breakpoint_ids.pop(breakpoint_id, None)
            self._append_action(
                runtime,
                "clear_breakpoint",
                f"已清除源码断点 {breakpoint.path}:{breakpoint.line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def install_breakpoint(
        self,
        runtime: NodeDebugRuntime,
        breakpoint: NodeDebugBreakpointDTO,
        *,
        script_path: Path | None = None,
    ) -> None:
        target_path = script_path or safe_join(runtime.workspace_root, breakpoint.path)
        condition = inspector_breakpoint_condition(
            breakpoint_id=breakpoint.breakpoint_id,
            condition=breakpoint.condition,
            hit_condition=breakpoint.hit_condition,
            log_message=breakpoint.log_message,
        )
        result = await self._command(
            runtime,
            "Debugger.setBreakpointByUrl",
            {
                "url": target_path.as_uri(),
                "lineNumber": breakpoint.line - 1,
                "columnNumber": breakpoint.column - 1,
                **({"condition": condition} if condition else {}),
            },
        )
        inspector_id = result.get("breakpointId")
        locations = result.get("locations")
        actual_line = None
        if isinstance(locations, list) and locations:
            location = locations[0]
            if isinstance(location, dict) and isinstance(
                location.get("lineNumber"), int
            ):
                actual_line = int(location["lineNumber"]) + 1
        if not isinstance(inspector_id, str):
            raise TypeError("Node Inspector 设置断点响应缺少 breakpointId")
        async with runtime.state_lock:
            runtime.inspector_breakpoint_ids[breakpoint.breakpoint_id] = inspector_id
            runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint.model_copy(
                update={
                    "verified": actual_line is not None,
                    "actual_line": actual_line,
                    "inspector_id": inspector_id,
                    "relocation_status": "current",
                    "relocation_message": None,
                }
            )

    @staticmethod
    def _matching_breakpoint(
        breakpoints: Iterable[NodeDebugBreakpointDTO],
        target: NodeDebugBreakpointDTO,
    ) -> NodeDebugBreakpointDTO | None:
        for breakpoint in breakpoints:
            if (
                breakpoint.path == target.path
                and breakpoint.line == target.line
                and breakpoint.column == target.column
            ):
                return breakpoint
        return None
