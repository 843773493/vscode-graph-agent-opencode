"""Node Debug 工具动作记录链路。

承载 record_tool_action 这一条产品入口：把 Agent 工具调用结果映射为调试
动作时间线（同 tool_call_id 就地更新，否则按 source action 归并或追加），并
对 runtime 与 pending 两种承载分别落库。由 NodeDebugService 继承（宿主必须
提供 _runtimes、_session_state、_owner_key、_admit_mutation 与公开入口 get_state），
不反向依赖顶层 service.py。
"""

from __future__ import annotations

from typing import Literal

from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugStateDTO,
)

_TOOL_ACTION_SOURCES: dict[str, frozenset[str]] = {
    "create_debug_configuration": frozenset({"create_configuration"}),
    "activate_debug_configuration": frozenset({"activate_configuration"}),
    "delete_debug_configuration": frozenset({"delete_configuration"}),
    "start_debugging": frozenset({"start", "start_failed"}),
    "stop_debugging": frozenset({"stop"}),
    "restart_debugging": frozenset({"start", "start_failed"}),
    "continue_execution": frozenset({"continue"}),
    "pause_execution": frozenset({"pause"}),
    "step_over": frozenset({"step_over"}),
    "step_into": frozenset({"step_into"}),
    "step_out": frozenset({"step_out"}),
    "add_breakpoint": frozenset({"set_breakpoint"}),
    "add_logpoint": frozenset({"set_breakpoint"}),
    "remove_breakpoint": frozenset({"clear_breakpoint"}),
    "clear_all_breakpoints": frozenset({"clear_all_breakpoints"}),
    "evaluate_expression": frozenset({"evaluate"}),
}


class NodeDebugToolActionMixin:
    """工具动作记录链路的方法族（由 NodeDebugService 继承）。"""

    async def record_tool_action(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_call_id: str,
        result: Literal["success", "error"],
        message: str,
        thread_id: str,
        extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            pending = self._session_state.pending_actions(owner)
            existing_index = next(
                (
                    index
                    for index in range(len(pending) - 1, -1, -1)
                    if pending[index].tool_name == tool_name
                    and pending[index].tool_call_id == tool_call_id
                ),
                None,
            )
            if existing_index is not None:
                self._session_state.replace_pending_action(
                    owner,
                    existing_index,
                    pending[existing_index].model_copy(
                        update={
                            "message": message,
                            "result": result,
                            "actor": "ai",
                            "extension_catalog_binding": extension_catalog_binding,
                        }
                    ),
                )
            else:
                self._session_state.append_pending_action(
                    session_id,
                    thread_id,
                    tool_name,
                    message,
                    actor="ai",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    extension_catalog_binding=extension_catalog_binding,
                    result=result,
                )
            self._session_state.persist_runtime_state(
                session_id, thread_id, None
            )
            return await self.get_state(session_id, thread_id)
        async with runtime.state_lock:
            existing_index = next(
                (
                    index
                    for index in range(len(runtime.actions) - 1, -1, -1)
                    if runtime.actions[index].tool_name == tool_name
                    and runtime.actions[index].tool_call_id == tool_call_id
                ),
                None,
            )
            if existing_index is not None:
                existing = runtime.actions[existing_index]
                runtime.actions[existing_index] = existing.model_copy(
                    update={
                        "message": message,
                        "result": result,
                        "actor": "ai",
                        "extension_catalog_binding": extension_catalog_binding,
                    }
                )
            else:
                latest = runtime.actions[-1] if runtime.actions else None
                source_actions = _TOOL_ACTION_SOURCES.get(tool_name, frozenset())
                if (
                    latest is not None
                    and latest.tool_name is None
                    and latest.action in source_actions
                ):
                    runtime.actions[-1] = latest.model_copy(
                        update={
                            "message": message,
                            "actor": "ai",
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "extension_catalog_binding": extension_catalog_binding,
                            "result": result,
                        }
                    )
                else:
                    self._append_action(
                        runtime,
                        tool_name,
                        message,
                        actor="ai",
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        extension_catalog_binding=extension_catalog_binding,
                        result=result,
                    )
        self._session_state.persist_runtime_state(
            session_id, thread_id, runtime
        )
        return await self.get_state(session_id, thread_id)
