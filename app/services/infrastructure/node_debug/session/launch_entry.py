"""Node Debug 启动/重启入口链路。

承载 start/restart 两条产品入口与启动编排器的构造：owner 准入后在 per-owner
临界区内执行整段启动序列（核实旧 run → 登记 claim → 插件 launch → Inspector 握手）。
由 NodeDebugService 继承（宿主必须提供全部协作者与装配字段），不反向依赖顶层
service.py。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal

from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugBindingSnapshotDTO,
    NodeDebugBreakpointRequest,
    NodeDebugDebuggerContributionDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.node_debug.breakpoint.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
)
from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.process.launch_orchestrator import (
    NodeDebugLaunchContext,
    NodeDebugLaunchOrchestrator,
    NodeDebugLaunchRequest,
)
from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.process.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
)
from app.services.infrastructure.node_debug.session.thread_owner import NodeDebugOwner


class NodeDebugLaunchEntryMixin:
    """启动/重启入口链路的方法族（由 NodeDebugService 继承）。"""

    _workspace_root: Path
    _workspace_id: str | None
    _gateway_connection: GatewayTransportConnection
    _configuration_factory: NodeDebugConfigurationFactory
    _breakpoint_mutations: NodeDebugBreakpointMutations
    _inspector: NodeDebugInspector
    _lifecycle: NodeDebugProcessLifecycle
    _runtimes: dict[NodeDebugOwner, NodeDebugRuntime]
    _session_state: NodeDebugSessionState
    _configuration_registry: NodeDebugConfigurationRegistry
    _runtimes_lock: asyncio.Lock
    _claim_runtime: NodeDebugClaimRuntime
    _observer: NodeDebugRuntimeObserver
    _admit_mutation: Callable[
        [str, str | None], AbstractAsyncContextManager[NodeDebugOwner]
    ]
    _get_state_admitted: Callable[[str, str], Awaitable[NodeDebugStateDTO]]

    async def resolve_node_debugger_binding(
        self, *, registration_id: str | None, gateway_id: str, request_id: str,
    ) -> NodeDebugDebuggerContributionDTO: ...

    async def start(
        self,
        *,
        session_id: str,
        path: str,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        thread_id: str | None = None,
        configuration_id: str | None = None,
        launch_profile_name: str | None = None,
        working_directory: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        debugger_registration_id: str | None = None,
        defer_configuration_done: bool = False,
        gateway_id: str | None = None,
        request_id: str | None = None,
    ) -> NodeDebugStateDTO:
        """公开启动入口：Session 准入后，按 owner 串行执行整段启动序列。

        per-owner 临界区覆盖 claim、插件 launch、Inspector 握手与 running 发布。
        同一临界区也覆盖 ``apply_action("stop")``、``restart()`` 与 ``close()``，
        避免并发操作覆盖同一 owner 的 run 登记或在真实退出前发布终态。临界区内
        等待覆盖原副作用和账本worker的真实结算，不能提前释放锁或伪报停止。
        TODO(5.4)：原生I/O抢占及整个启动/关闭期限尚未收口。
        锁序保持 ``_owner_lock → _runtimes_lock → runtime.state_lock``。
        """
        async with self._admit_mutation(session_id, thread_id) as admitted_owner:
            session_id, thread_id = admitted_owner
            owner = self._owner_key(session_id, thread_id)
            gateway_id = gateway_id or get_current_gateway_id()
            request_id = request_id or get_current_request_id()
            if not gateway_id or not request_id:
                raise RuntimeError("Node Debug 启动缺少当前 Gateway 或 request_id")
            contribution = await self.resolve_node_debugger_binding(
                registration_id=debugger_registration_id,
                gateway_id=gateway_id,
                request_id=request_id,
            )
            debugger_binding = NodeDebugBindingSnapshotDTO.model_validate(
                contribution.model_dump(mode="json")
            )
            async with self._owner_lock(owner):
                result = await self._create_launch_orchestrator().launch(
                    NodeDebugLaunchRequest(
                        owner=owner,
                        path=path,
                        args=args,
                        breakpoints=breakpoints,
                        configuration_id=configuration_id,
                        launch_profile_name=launch_profile_name,
                        working_directory=working_directory,
                        actor=actor,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        debugger_registration_id=contribution.registration_id,
                        defer_configuration_done=defer_configuration_done,
                        debugger_binding=debugger_binding,
                        request_id=request_id,
                    )
                )
                return await self._get_state_admitted(*result.owner)

    async def restart(
        self,
        session_id: str,
        *,
        thread_id: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        gateway_id: str | None = None,
        request_id: str | None = None,
    ) -> NodeDebugStateDTO:
        async with self._admit_mutation(session_id, thread_id) as admitted_owner:
            session_id, thread_id = admitted_owner
            owner = self._owner_key(session_id, thread_id)
            # 重启 = 停止旧实例 + 启动新实例，必须整体处于 per-owner 临界区内（R3b 复核
            # 残留项）：否则 stop 落在另一并发启动的 spawn 窗口时会得到假终态。临界区内
            # 直接调用启动编排器，绝不能再经 `start()` 重入同一把 asyncio.Lock
            # （不可重入，重入即自锁）。
            async with self._owner_lock(owner):
                self._session_state.ensure_loaded(session_id, thread_id)
                runtime = self._runtimes.get(owner)
                if runtime is None:
                    self._assert_no_unsettled_claim(owner, operation="重启调试")
                    raise RuntimeError(
                        f"Node 调试会话不存在: session_id={session_id}, thread_id={thread_id}"
                    )
                frozen_binding = runtime.debugger_binding
                if frozen_binding is None:
                    raise RuntimeError("无法从原 Node Debug run 恢复冻结的插件绑定")
                resolved_gateway_id = gateway_id or frozen_binding.gateway_id
                resolved_request_id = request_id or get_current_request_id()
                if not resolved_request_id:
                    raise RuntimeError("Node Debug 重启缺少当前 request_id")
                contribution = await self.resolve_node_debugger_binding(
                    registration_id=frozen_binding.registration_id,
                    gateway_id=resolved_gateway_id,
                    request_id=resolved_request_id,
                )
                if contribution.gateway_id != frozen_binding.gateway_id:
                    raise RuntimeError("Node Debug 重启目标 Gateway 与原 run 不匹配")
                debugger_binding = NodeDebugBindingSnapshotDTO.model_validate(
                    contribution.model_dump(mode="json")
                )
                async with runtime.state_lock:
                    path = runtime.relative_script_path
                    args = list(runtime.args)
                    configuration_id = runtime.configuration_id
                    launch_profile_name = runtime.launch_profile_name
                    working_directory = (
                        str(runtime.working_directory)
                        if runtime.working_directory is not None
                        else ""
                    )
                    breakpoints = [
                        NodeDebugBreakpointRequest(
                            path=breakpoint.path,
                            line=breakpoint.line,
                            column=breakpoint.column,
                            condition=breakpoint.condition,
                            hit_condition=breakpoint.hit_condition,
                            log_message=breakpoint.log_message,
                        )
                        for breakpoint in runtime.breakpoints.values()
                        if breakpoint.relocation_status == "current"
                    ]
                outcome = await self._lifecycle.stop_runtime(runtime)
                if outcome == "reconcile_required":
                    # 旧实例无法核实终态：保持 reconcile_required，绝不为同一 owner 启动新实例。
                    self._session_state.persist_runtime_state(
                        session_id, thread_id, runtime
                    )
                    raise RuntimeError(
                        "旧调试实例无法核实终态，保持 reconcile_required；拒绝重启: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
                async with runtime.state_lock:
                    runtime.status = "exited"
                result = await self._create_launch_orchestrator().launch(
                    NodeDebugLaunchRequest(
                        owner=owner,
                        path=path,
                        args=args,
                        breakpoints=breakpoints,
                        configuration_id=configuration_id,
                        launch_profile_name=launch_profile_name,
                        working_directory=working_directory,
                        actor=actor,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        debugger_registration_id=debugger_binding.registration_id,
                        debugger_binding=debugger_binding,
                        request_id=resolved_request_id,
                    )
                )
                return await self._get_state_admitted(*result.owner)

    def _create_launch_orchestrator(self) -> NodeDebugLaunchOrchestrator:
        """为本次启动读取最新 service 回调，避免缓存旧的 monkeypatch/运行态。"""
        return NodeDebugLaunchOrchestrator(
            NodeDebugLaunchContext(
                workspace_root=self._workspace_root,
                workspace_id=self._workspace_id,
                gateway_connection=self._gateway_connection,
                configuration_factory=self._configuration_factory,
                breakpoint_mutations=self._breakpoint_mutations,
                inspector=self._inspector,
                lifecycle=self._lifecycle,
                runtimes=self._runtimes,
                session_state=self._session_state,
                set_selection=self._configuration_registry.set_selection,
                runtimes_lock=self._runtimes_lock,
                max_actions=MAX_NODE_DEBUG_ACTIONS,
                load_session=self._session_state.ensure_loaded,
                reconcile_sources=self._reconcile_session_sources,
                select_configuration=self._configuration_registry.select_for_start,
                read_configuration=self._configuration_registry.get,
                read_runtime_config=self._get_typed_debug_runtime_config,
                read_source_digests=self._source_digests_for_runtime,
                persist_state=self._session_state.persist_runtime_state,
                write_claim=self._claim_runtime.write_launch_claim,
                mark_claim_running=self._claim_runtime.mark_claim_running,
                append_action=self._append_action,
                is_paused_at_breakpoint=self._observer.paused_at_breakpoint,
                monitor_process=self._observer.monitor_process,
                terminal_error_message=self._observer.terminal_error_message,
            )
        )
