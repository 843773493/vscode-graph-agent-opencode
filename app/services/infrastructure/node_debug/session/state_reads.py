"""Node Debug 状态读取入口链路。

承载 get_state/get_variables 两条只读入口：冷读取时按持久 claim 核实旧实例、
对账 source、并组装脱敏快照。由 NodeDebugService 继承（宿主必须提供
_runtimes、_session_state、_configuration_registry、_claim_runtime、
_admit_mutation、_reconcile_session_sources 与快照装配），
不反向依赖顶层 service.py。
"""

from __future__ import annotations

from app.schemas.internal_v2.node_debug import (
    NodeDebugStateDTO,
    NodeDebugVariableDTO,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    build_node_debug_snapshot,
)


class NodeDebugStateReadMixin:
    """状态读取入口链路的方法族（由 NodeDebugService 继承）。"""

    async def get_state(
        self, session_id: str, thread_id: str | None = None
    ) -> NodeDebugStateDTO:
        async with self._admit_mutation(session_id, thread_id) as owner:
            return await self._get_state_admitted(*owner)

    async def _get_state_admitted(
        self, session_id: str, thread_id: str
    ) -> NodeDebugStateDTO:
        """读取状态并对账；调用方必须已持有该 Session 准入。"""
        owner = (session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
        if runtime is None:
            selection = self._configuration_registry.selection(session_id, thread_id)
            claim = self._claim_runtime.active_claim(session_id, thread_id)
            return NodeDebugStateDTO(
                session_id=session_id,
                thread_id=thread_id,
                status=(
                    "reconcile_required"
                    if claim is not None and claim.phase == "reconcile_required"
                    else "idle"
                ),
                error_message=(
                    claim.reconcile_reason
                    if claim is not None and claim.phase == "reconcile_required"
                    else None
                ),
                active_configuration_id=self._configuration_registry.active_id(
                    session_id, thread_id
                ),
                active_configuration_name=self._configuration_registry.active_name(
                    session_id, thread_id
                ),
                configurations=self._configuration_registry.summaries(
                    session_id, thread_id
                ),
                script_path=selection.script_path,
                working_directory=selection.working_directory,
                launch_profile_name=selection.launch_profile_name,
                args=list(selection.args),
                breakpoints=[
                    breakpoint.model_copy(deep=True)
                    for breakpoint in self._session_state.pending_breakpoints(owner)
                ],
                actions=[
                    action.model_copy(deep=True)
                    for action in self._session_state.pending_actions(owner)
                ],
                configuration_revision=(
                    self._configuration_registry.active_revision(session_id, thread_id)
                ),
            )
        async with runtime.state_lock:
            return build_node_debug_snapshot(
                runtime,
                self._configuration_registry,
            )

    async def get_variables(
        self,
        *,
        session_id: str,
        thread_id: str | None = None,
        variable_names: list[str] | None = None,
        scope: str = "all",
    ) -> list[NodeDebugVariableDTO]:
        async with self._admit_mutation(session_id, thread_id) as owner:
            state = await self._get_state_admitted(*owner)
        if state.status != "paused" or not state.call_stack:
            raise RuntimeError("只有暂停在源码断点时才能检查变量")
        if scope not in {"local", "global", "all"}:
            raise ValueError(f"不支持的变量 scope: {scope}")
        requested = set(variable_names or [])
        result: list[NodeDebugVariableDTO] = []
        for variable in state.call_stack[0].variables:
            if scope != "all" and variable.scope != scope:
                continue
            if requested and variable.name not in requested:
                continue
            result.append(variable.model_copy(deep=True))
        if variable_names:
            found = {variable.name for variable in result}
            missing = [name for name in variable_names if name not in found]
            if missing:
                raise ValueError("当前暂停上下文找不到指定变量: " + ", ".join(missing))
        return result
