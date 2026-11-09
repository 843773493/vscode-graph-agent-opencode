from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from app.core.trace_middleware import GATEWAY_ID_HEADER, require_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.schemas.internal_v2.node_debug import (
    NodeDebugDebuggerContributionsDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.node_debug.breakpoint.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.breakpoint.source_reconciliation import (
    NodeDebugSourceReconciliation,
)
from app.services.infrastructure.node_debug.configuration.configuration_control import (
    NodeDebugConfigurationControlMixin,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.configuration.runtime_config import (
    NodeDebugRuntimeConfig,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
)
from app.services.infrastructure.node_debug.process.evaluation import (
    NodeDebugEvaluation,
)
from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.process.launch_claim import (
    NodeDebugClaimRecoveryDecision,
)
from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.process.plugin_runtime import (
    NodeDebugPluginRuntime,
)
from app.services.infrastructure.node_debug.process.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.action_dispatch import (
    NodeDebugActionDispatchMixin,
)
from app.services.infrastructure.node_debug.session.launch_entry import (
    NodeDebugLaunchEntryMixin,
)
from app.services.infrastructure.node_debug.session.lifecycle_entry import (
    NodeDebugLifecycleEntryMixin,
)
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
    append_runtime_debug_action,
)
from app.services.infrastructure.node_debug.session.state_reads import (
    NodeDebugStateReadMixin,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
    resolve_node_debug_owner,
)
from app.services.infrastructure.node_debug.session.tool_actions import (
    NodeDebugToolActionMixin,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.orchestration.thread_residency import (
    ResidencyBlocker,
    ThreadResidencyTracker,
)

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService

logger = logging.getLogger(__name__)


class NodeDebugService(
    NodeDebugActionDispatchMixin,
    NodeDebugToolActionMixin,
    NodeDebugLaunchEntryMixin,
    NodeDebugStateReadMixin,
    NodeDebugLifecycleEntryMixin,
    NodeDebugConfigurationControlMixin,
):
    """通过 Node Inspector 提供 SessionThread 级 JavaScript 源码调试。

    运行时、断点、活动方案和动作时间线全部以精确 ``(session_id, thread_id)``
    owner key 隔离。所有显式入口都要求 canonical ``thread_id``；Session-only
    入口必须先通过 catalog 取得 ``main_thread_id(session_id)``，NodeDebug 不接收
    ``main``、Session ID 或 child Session 别名。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        file_paths: WorkspaceFilePaths,
        config_service: ConfigService,
        session_store: NodeDebugSessionStore | None = None,
        session_admission: NodeDebugSessionAdmission,
        external_resource_leases: ExternalResourceLeaseLedger,
        residency_tracker: ThreadResidencyTracker | None = None,
        state_events: ResourceStateEventPublisher | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._file_paths = file_paths
        self._workspace_id = workspace_id
        self._config_service = config_service
        self._gateway_connection = GatewayTransportConnection(config_service=config_service)
        self._session_store = session_store
        #: 唯一 external_resource_leases 账本。只登记/结清 typed ``node_debug_process``
        #: 占用，不参与任何进程状态判断（见 claim_runtime 的职责说明）。
        self._external_resource_leases = external_resource_leases
        #: ThreadResidency 的 blocker 上报目标（R5b）：把已核实的 claim 相位变化单向
        #: 推送为 thread 的 idle blocker；本服务绝不反向读取 residency 推断进程状态。
        self._residency_tracker = residency_tracker
        #: resource.state/{owner_domain} 轻量通知出口：只在 owner 已核实的
        #: 释放失败（进入 reconcile_required）时发布 release_failed；成功终态
        #: 由账本 settle 发布 released。通知失败不改变 durable claim 事实。
        self._state_events = state_events
        self._append_action = partial(
            append_runtime_debug_action, max_actions=MAX_NODE_DEBUG_ACTIONS
        )
        self._runtimes: dict[NodeDebugOwner, NodeDebugRuntime] = {}
        self._closing = False
        self._admitted_operations: dict[NodeDebugOwner, set[asyncio.Future[None]]] = {}
        self._close_tasks: dict[NodeDebugOwner, asyncio.Task[None]] = {}
        #: per-owner 启动临界区：串行化“核实旧 claim → durable 登记 → spawn → 握手”。
        #: 条目数与 ``_runtimes`` 同量级（每个被触达过的 owner 一把锁）；不做回收，
        #: 以免丢弃仍被并发任务持有的锁。
        self._owner_locks: dict[NodeDebugOwner, asyncio.Lock] = {}
        self._configuration_factory = NodeDebugConfigurationFactory(
            file_paths=file_paths,
        )
        self._configuration_registry = NodeDebugConfigurationRegistry(
            store=session_store,
            configuration_factory=self._configuration_factory,
        )
        self._session_state = NodeDebugSessionState(
            configuration_registry=self._configuration_registry,
            runtimes=self._runtimes,
            store=session_store,
        )
        self._session_admission = session_admission
        # 持久化入口由同一个 catalog resolver 校验 thread 归属；无持久化的
        # 单测只校验 canonical ID 形态。
        self._thread_path_resolver = (
            session_store.path_resolver if session_store is not None else None
        )
        self._runtimes_lock = asyncio.Lock()
        self._inspector = NodeDebugInspector(
            workspace_root=self._workspace_root,
            file_paths=file_paths,
            append_action=self._append_action,
            clear_stop_snapshot=self._clear_stop_snapshot,
        )
        self._breakpoint_mutations = NodeDebugBreakpointMutations(
            workspace_root=self._workspace_root,
            configuration_factory=self._configuration_factory,
            session_state=self._session_state,
            command=self._inspector.command,
            append_action=self._append_action,
            append_pending_action=self._session_state.append_pending_action,
        )
        self._source_reconciliation = NodeDebugSourceReconciliation(
            file_paths=file_paths,
            session_state=self._session_state,
            command=self._inspector.command,
            append_action=self._append_action,
        )
        self._evaluation = NodeDebugEvaluation(
            inspector=self._inspector,
            append_action=self._append_action,
        )
        self._claim_runtime = NodeDebugClaimRuntime(
            session_store=self._session_store,
            external_resource_leases=self._external_resource_leases,
            runtimes=self._runtimes,
            residency_tracker=self._residency_tracker,
            state_events=self._state_events,
        )
        self._lifecycle = NodeDebugProcessLifecycle(
            runtimes=self._runtimes,
            read_launch_claim=self._claim_runtime.read_launch_claim,
            write_launch_claim=self._claim_runtime.write_launch_claim,
            mark_claim_phase=self._claim_runtime.mark_claim_phase,
            settle_process_lease=self._claim_runtime.settle_process_lease,
            append_action=self._append_action,
            append_pending_action=self._session_state.append_pending_action,
            write_session_manifest=self._session_state.write_session_manifest,
            clear_stop_snapshot=self._clear_stop_snapshot,
            recover_run=self._recover_debug_run,
        )
        self._observer = NodeDebugRuntimeObserver(
            mark_claim_phase=self._claim_runtime.mark_claim_phase,
            clear_stop_snapshot=self._clear_stop_snapshot,
            append_action=self._append_action,
        )

    async def _recover_debug_run(self, claim) -> NodeDebugClaimRecoveryDecision:
        runtime = await NodeDebugPluginRuntime.connect(
            binding=claim.debugger_binding,
            workspace_id=claim.debugger_binding.workspace_id,
            connection=self._gateway_connection,
            request_id=require_current_request_id("Node Debug run 恢复"),
            run_id=claim.run_id,
        )
        attached = False
        try:
            status = await runtime.status()
            if (status.get("run_id") != claim.run_id
                    or status.get("process_instance_id") != claim.process_instance_id):
                return NodeDebugClaimRecoveryDecision(
                    "execution_lost", "原 Debug 插件状态与冻结的 run/process_instance 不匹配"
                )
            if status.get("status") == "exited":
                return NodeDebugClaimRecoveryDecision("settled", "原 Debug 插件确认目标 run 已退出")
            if status.get("status") != "running":
                return NodeDebugClaimRecoveryDecision(
                    "execution_lost", f"原 Debug 插件返回未知 run 状态: {status.get('status')!r}"
                )

            script_path, relative_path = self._configuration_factory.resolve_script_path(claim.script_path)
            working_directory = self._configuration_factory.resolve_working_directory(claim.working_directory)
            target_pid = status.get("pid")
            if not isinstance(target_pid, int) or isinstance(target_pid, bool):
                target_pid = claim.pid
            recovered = NodeDebugRuntime(
                session_id=claim.session_id,
                thread_id=claim.thread_id,
                configuration_id=claim.configuration_id,
                debugger_binding=claim.debugger_binding,
                workspace_root=self._workspace_root,
                script_path=script_path,
                relative_script_path=relative_path,
                run_id=claim.run_id,
                args=list(claim.args),
                working_directory=working_directory,
                launch_profile_name=claim.launch_profile_name,
                plugin_runtime=runtime,
                target_pid=target_pid,
                process_instance_id=claim.process_instance_id,
                status="running",
            )
            recovered.breakpoints = {
                item.breakpoint_id: item.model_copy(deep=True) for item in claim.breakpoints
            }
            await self._inspector.connect(recovered)
            await self._inspector.command(recovered, "Runtime.enable")
            await self._inspector.command(recovered, "Debugger.enable")
            await self._inspector.command(
                recovered,
                "NodeRuntime.notifyWhenWaitingForDisconnect",
                {"enabled": True},
            )
            inspector_state = status.get("inspector_state")
            if isinstance(inspector_state, dict):
                method = inspector_state.get("method")
                params = inspector_state.get("params")
                if method == "Debugger.paused" and isinstance(params, dict):
                    await self._inspector._handle_event(recovered, method, params)
            recovered.process_task = asyncio.create_task(self._observer.monitor_process(recovered))
            owner = (claim.session_id, claim.thread_id)
            async with self._runtimes_lock:
                self._runtimes[owner] = recovered
            attached = True
            return NodeDebugClaimRecoveryDecision("running", "已按冻结绑定重新接入原 Debug run")
        finally:
            if not attached:
                await runtime.close()

    async def get_debugger_contributions(
        self, *, workspace_id: str, gateway_id: str, request_id: str,
    ) -> NodeDebugDebuggerContributionsDTO:
        if self._workspace_id is None or workspace_id != self._workspace_id:
            raise ValueError("Debug 声明查询 workspace_id 与当前 Backend owner 不匹配")
        base_url = self._gateway_connection.resolve_gateway_url()
        url = f"{base_url}/api/gateway/service-plugins/debugger-contributions"
        headers = {
            "X-Local-Token": get_gateway_local_token(),
            "X-BoxTeam-Workspace-Id": workspace_id,
            "X-BoxTeam-Target-Gateway-Id": gateway_id,
            GATEWAY_ID_HEADER: gateway_id,
            "X-Request-ID": request_id,
        }
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=self._gateway_connection.resolve_timeout_seconds(),
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    url,
                    params={"workspace_id": workspace_id},
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            detail = error.response.text
            raise RuntimeError(
                "读取 Gateway Debug 声明失败: "
                f"status={error.response.status_code}, request_id={request_id}, detail={detail}"
            ) from error
        except httpx.RequestError as error:
            raise ConnectionError(
                f"连接 Workspace Gateway 读取 Debug 声明失败: request_id={request_id}, "
                f"error_type={type(error).__name__}"
            ) from error
        try:
            envelope = response.json()
        except ValueError as error:
            raise TypeError("Gateway Debug 声明响应不是合法 JSON") from error
        if not isinstance(envelope, dict) or envelope.get("request_id") != request_id:
            raise ValueError("Gateway Debug 声明响应缺少原 request_id")
        raw_data = envelope.get("data")
        result = NodeDebugDebuggerContributionsDTO.model_validate(raw_data)
        if result.gateway_id != gateway_id or result.workspace_id != workspace_id:
            raise ValueError("Gateway Debug 声明响应目标身份与请求不匹配")
        return result

    async def resolve_node_debugger_binding(
        self, *, gateway_id: str, request_id: str, registration_id: str | None = None,
    ):
        if self._workspace_id is None:
            raise RuntimeError("当前 Backend 未绑定 workspace，不能选择 Debug 插件")
        contributions = await self.get_debugger_contributions(
            workspace_id=self._workspace_id,
            gateway_id=gateway_id,
            request_id=request_id,
        )
        candidates = [item for item in contributions.debuggers if item.type == "node"]
        if registration_id is not None:
            candidates = [item for item in candidates if item.registration_id == registration_id]
            if not candidates:
                raise ValueError("冻结的 Node Debug 插件注册已失效或不属于当前 Gateway／Workspace")
        if len(candidates) != 1:
            raise ValueError(
                "Node Debug 插件必须恰有一个 ready 目标；"
                f"gateway_id={gateway_id}, workspace_id={self._workspace_id}, count={len(candidates)}"
            )
        return candidates[0]

    def _owner_lock(self, owner: NodeDebugOwner) -> asyncio.Lock:
        """取（或懒建）该 owner 的临界区：串行化 start/stop/restart/close 的整段序列。

        asyncio 单线程事件循环内 check-then-set 之间没有 await，不存在竞态。
        旧实例停止的 terminate/kill 核实各有期限；账本worker须真实结算后才释放本锁。
        TODO(5.4)：原生I/O不可抢占，整个临界区与shutdown的总期限仍未收口。
        ``close()`` 先释放 ``_runtimes_lock`` 再取本锁，避免形成反向持锁。
        """
        lock = self._owner_locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._owner_locks[owner] = lock
        return lock

    async def clear_all_breakpoints(
        self,
        session_id: str,
        *,
        thread_id: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        async with self._admit_mutation(session_id, thread_id) as admitted_owner:
            session_id, thread_id = admitted_owner
            owner = self._owner_key(session_id, thread_id)
            self._session_state.ensure_loaded(session_id, thread_id)
            runtime = self._runtimes.get(owner)
            if runtime is None:
                removed = self._session_state.clear_pending_breakpoints(owner)
                self._session_state.append_pending_action(
                    session_id,
                    thread_id,
                    "clear_all_breakpoints",
                    f"已清除全部源码断点（{removed} 个）",
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
                self._session_state.persist_runtime_state(session_id, thread_id, None)
                return await self._get_state_admitted(session_id, thread_id)
            async with runtime.state_lock:
                breakpoint_ids = tuple(runtime.inspector_breakpoint_ids.values())
            for inspector_id in breakpoint_ids:
                if runtime.inspector.socket is not None:
                    await self._inspector.command(
                        runtime,
                        "Debugger.removeBreakpoint",
                        {"breakpointId": inspector_id},
                    )
            async with runtime.state_lock:
                runtime.breakpoints.clear()
                runtime.inspector_breakpoint_ids.clear()
                self._append_action(
                    runtime,
                    "clear_all_breakpoints",
                    "已清除全部源码断点",
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
            self._session_state.persist_runtime_state(session_id, thread_id, runtime)
            return await self._get_state_admitted(session_id, thread_id)

    @staticmethod
    def _owner_key(session_id: str, thread_id: str) -> NodeDebugOwner:
        """纯 owner key 归一，只用于精确 owner 参数，不触碰目录索引。"""
        return normalize_node_debug_owner(session_id, thread_id)

    def _resolve_owner(
        self,
        session_id: str,
        thread_id: str | None,
    ) -> NodeDebugOwner:
        """读入口按 catalog 解析精确 canonical SessionThread owner。"""
        if self._thread_path_resolver is None:
            if thread_id is None:
                raise RuntimeError(
                    "Session-only Node 调试入口要求 catalog path resolver"
                )
            return self._owner_key(session_id, thread_id)
        resolved_thread_id = (
            self._thread_path_resolver.main_thread_id(session_id)
            if thread_id is None
            else thread_id
        )
        return resolve_node_debug_owner(
            self._thread_path_resolver,
            session_id=session_id,
            thread_id=resolved_thread_id,
        ).key

    @asynccontextmanager
    async def _admit_mutation(
        self,
        session_id: str,
        thread_id: str | None,
    ) -> AsyncIterator[NodeDebugOwner]:
        """整个调试 mutation 期间持有 Session 生命周期准入。"""
        if self._closing:
            raise RuntimeError("Node 调试服务正在关闭，拒绝新操作")
        completed: asyncio.Future[None] | None = None
        owner: NodeDebugOwner | None = None
        try:
            async with self._session_admission.admit(session_id, thread_id) as admitted:
                # Session gate 等待期间也可能开始关闭；副作用前重新核准入。
                if self._closing:
                    raise RuntimeError("Node 调试服务正在关闭，拒绝新操作")
                owner = admitted.key
                completed = asyncio.get_running_loop().create_future()
                self._admitted_operations.setdefault(owner, set()).add(completed)
                # 冷恢复也会等待持久提交；与同 owner 的启动、停止及其它恢复串行。
                # 活跃 runtime 的只读快照仍能在进程停止等待期间推进。
                if self._runtimes.get(owner) is None:
                    async with self._owner_lock(owner):
                        await self._lifecycle.reconcile_persisted_claim(owner)
                yield owner
        finally:
            # Session gate 与取消收尾均已结算，关闭任务才能核实该 owner 最终的 runtime。
            if completed is not None and owner is not None:
                completed.set_result(None)
                operations = self._admitted_operations[owner]
                operations.remove(completed)
                if not operations:
                    del self._admitted_operations[owner]

    def residency_blockers(
        self, session_id: str, thread_id: str
    ) -> list[ResidencyBlocker]:
        """从 claim/runtime 投影读取该 owner 当前仍活跃的占用。"""
        owner = self._owner_key(session_id, thread_id)
        return self._claim_runtime.residency_blockers(*owner)

    def _assert_no_unsettled_claim(
        self, owner: NodeDebugOwner, *, operation: str
    ) -> None:
        """未结清的 durable claim（含 reconcile_required）必须阻断 owner 级操作。"""
        claim = self._claim_runtime.active_claim(*owner)
        if claim is None:
            return
        raise RuntimeError(
            f"{operation}被未结清的调试实例登记阻断: "
            f"phase={claim.phase}, session_id={owner[0]}, thread_id={owner[1]}, "
            f"reason={claim.reconcile_reason or '等待核实旧实例终态'}"
        )

    def _source_digests_for_runtime(
        self,
        runtime: NodeDebugRuntime,
    ) -> dict[str, str | None]:
        return self._source_reconciliation.source_digests(runtime)

    async def _reconcile_session_sources(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        await self._source_reconciliation.reconcile(session_id, thread_id, runtime)

    def _get_typed_debug_runtime_config(self) -> NodeDebugRuntimeConfig:
        return NodeDebugRuntimeConfig.from_mapping(
            self._config_service.get_debug_runtime_config()
        )

    def _clear_stop_snapshot(self, runtime: NodeDebugRuntime) -> None:
        """清空 Inspector 暂停快照并把断点回退为未安装态。"""
        self._inspector.clear_paused_snapshot(runtime)
        runtime.inspector_breakpoint_ids.clear()
        runtime.breakpoints = {
            breakpoint_id: breakpoint.model_copy(
                update={
                    "verified": False,
                    "actual_line": None,
                }
            )
            for breakpoint_id, breakpoint in runtime.breakpoints.items()
        }
