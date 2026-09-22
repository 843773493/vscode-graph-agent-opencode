from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugActionRequest,
    NodeDebugBreakpointRequest,
    NodeDebugCapabilitiesDTO,
    NodeDebugClearBreakpointActionRequest,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugControlActionRequest,
    NodeDebugEvaluateActionRequest,
    NodeDebugLaunchProfileDTO,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugStateDTO,
    NodeDebugUpdateBreakpointActionRequest,
    NodeDebugVariableDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoint_expressions import (
    parse_logpoint_error,
    parse_logpoint_output,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.breakpoint.source_reconciliation import (
    NodeDebugSourceReconciliation,
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
from app.services.infrastructure.node_debug.process.launch_orchestrator import (
    NodeDebugLaunchContext,
    NodeDebugLaunchOrchestrator,
    NodeDebugLaunchRequest,
)
from app.services.infrastructure.node_debug.process.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
    resolve_node_debug_owner,
)
from app.services.orchestration.thread_residency import (
    ResidencyBlocker,
    ThreadResidencyTracker,
)

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
    append_runtime_debug_action,
    build_node_debug_snapshot,
)

_INSPECTOR_URL_PATTERN = re.compile(r"Debugger listening on (ws://\S+)")

_MAX_OUTPUT_LINES = 100
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
class NodeDebugService:
    """通过 Node Inspector 提供 SessionThread 级 JavaScript 源码调试。

    运行时、断点、活动方案和动作时间线全部以精确 ``(session_id, thread_id)``
    owner key 隔离。所有产品入口都要求显式 ``thread_id``；main thread 使用值
    ``"main"``，owner 别名折叠只由 :mod:`node_debug_thread_owner` 定义。
    同一实体的别名地址（``(parent_session, child_session)`` 与
    ``(child_session, main)``）在入口折叠为同一个 owner key。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        config_service: ConfigService,
        session_store: NodeDebugSessionStore | None = None,
        session_admission: NodeDebugSessionAdmission,
        external_resource_leases: ExternalResourceLeaseLedger,
        residency_tracker: ThreadResidencyTracker | None = None,
        state_events: ResourceStateEventPublisher | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._config_service = config_service
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
        self._runtimes: dict[NodeDebugOwner, NodeDebugRuntime] = {}
        #: per-owner 启动临界区：串行化“核实旧 claim → durable 登记 → spawn → 握手”。
        #: 条目数与 ``_runtimes`` 同量级（每个被触达过的 owner 一把锁）；不做回收，
        #: 以免丢弃仍被并发任务持有的锁。
        self._owner_locks: dict[NodeDebugOwner, asyncio.Lock] = {}
        self._configuration_factory = NodeDebugConfigurationFactory(
            workspace_root=self._workspace_root
        )
        self._configuration_registry = NodeDebugConfigurationRegistry(
            store=session_store,
            configuration_factory=self._configuration_factory,
        )
        self._session_state = NodeDebugSessionState(
            configuration_registry=self._configuration_registry,
            store=session_store,
        )
        self._session_admission = session_admission
        # 入口别名折叠需要目录索引；无持久化会话树场景（嵌入式/单测）没有可折叠的
        # thread 节点，只做 owner 归一。
        self._thread_path_resolver = (
            session_store.path_resolver if session_store is not None else None
        )
        self._runtimes_lock = asyncio.Lock()
        self._node_bin = os.environ.get("BOXTEAM_NODE_BIN") or shutil.which("node")
        self._inspector = NodeDebugInspector(
            workspace_root=self._workspace_root,
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
            workspace_root=self._workspace_root,
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
            notify_release_failed=self._claim_runtime.notify_release_failed,
            append_action=self._append_action,
            append_pending_action=self._session_state.append_pending_action,
            write_session_manifest=self._session_state.write_session_manifest,
            clear_stop_snapshot=self._clear_stop_snapshot,
        )

    def _create_launch_orchestrator(self) -> NodeDebugLaunchOrchestrator:
        """为本次启动读取最新 service 回调，避免缓存旧的 monkeypatch/运行态。"""
        return NodeDebugLaunchOrchestrator(
            NodeDebugLaunchContext(
                workspace_root=self._workspace_root,
                node_bin=self._node_bin,
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
                is_paused_at_breakpoint=self._paused_at_breakpoint,
                read_stream=self._read_stream,
                monitor_process=self._monitor_process,
            )
        )

    async def get_state(
        self, session_id: str, thread_id: str
    ) -> NodeDebugStateDTO:
        owner = self._resolve_owner(session_id, thread_id)
        session_id, thread_id = owner
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            # 冷读取时必须先按持久 claim 核实旧实例，绝不虚报 idle/终态。
            await self._lifecycle.reconcile_persisted_claim(owner)
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
            return self._snapshot(runtime)

    def get_capabilities(self) -> NodeDebugCapabilitiesDTO:
        """返回供客户端选择启动配置的脱敏调试能力。"""
        debug_config = self._get_typed_debug_runtime_config()
        profiles: list[NodeDebugLaunchProfileDTO] = []
        for name, profile in debug_config.launch_profiles.items():
            profiles.append(
                NodeDebugLaunchProfileDTO(
                    name=name,
                    adapter=profile.adapter,
                    runtime=profile.runtime,
                    supported=(
                        profile.adapter == "node_inspector"
                        and profile.runtime == "node"
                    ),
                    program=profile.program,
                    working_directory=profile.working_directory,
                    args=list(profile.args),
                )
            )
        return NodeDebugCapabilitiesDTO(
            enabled=debug_config.enabled,
            default_adapter=debug_config.default_adapter,
            supported_adapters=["node_inspector"],
            launch_profiles=profiles,
        )

    def resolve_launch_profile_name(self, launch_profile_name: str | None) -> str:
        """把方案/请求里的 profile 名称解析为实际生效的 profile 名称。

        Agent 工具面需要在启动前核对“显式 profile 与方案解析结果一致”，
        因此复用唯一的 typed 启动配置解析规则，避免在工具层
        复制默认 profile 名称形成第二套语义。本方法只读配置，不触碰运行时。
        """
        resolved_name, _ = self._get_typed_debug_runtime_config().resolve_profile(
            launch_profile_name
        )
        return resolved_name

    def list_configurations(
        self,
        session_id: str,
        thread_id: str,
    ) -> list[NodeDebugConfigurationDTO]:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration_registry.list(session_id, thread_id)

    def get_configuration(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration_registry.get(
            session_id, thread_id, configuration_id
        ).model_copy(deep=True)

    async def create_configuration(
        self,
        request: NodeDebugConfigurationCreateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(
            request.session_id, request.thread_id
        )
        self._session_state.ensure_loaded(session_id, thread_id)
        if request.activate:
            self._assert_no_running_target(session_id, thread_id)
        configuration = self._configuration_registry.create(
            request.model_copy(
                update={"session_id": session_id, "thread_id": thread_id}
            ),
        )
        if request.activate:
            self._session_state.sync_active_configuration(session_id, thread_id)
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "create_configuration",
            f"已创建调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def update_configuration(
        self,
        configuration_id: str,
        request: NodeDebugConfigurationUpdateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(
            request.session_id, request.thread_id
        )
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.get(session_id, thread_id, configuration_id)
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        replacement = self._configuration_registry.update(
            configuration_id,
            request.model_copy(
                update={"session_id": session_id, "thread_id": thread_id}
            ),
        )
        self._session_state.sync_active_configuration(session_id, thread_id)
        self._record_session_action(
            session_id,
            "update_configuration",
            f"已更新调试方案 {replacement.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def activate_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        configuration = self._configuration_registry.get(
            session_id, thread_id, configuration_id
        )
        self._assert_no_running_target(session_id, thread_id)
        self._configuration_registry.activate(
            session_id,
            configuration_id,
            thread_id=thread_id,
        )
        self._session_state.sync_active_configuration(session_id, thread_id)
        self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "activate_configuration",
            f"已激活调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def delete_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        configuration = self._configuration_registry.get(
            session_id, thread_id, configuration_id
        )
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        was_active = (
            self._configuration_registry.active_id(session_id, thread_id)
            == configuration_id
        )
        self._configuration_registry.remove(
            session_id,
            configuration_id,
            thread_id,
        )
        if was_active:
            self._session_state.clear_pending_breakpoints((session_id, thread_id))
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "delete_configuration",
            f"已删除调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def import_configuration(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
        *,
        thread_id: str,
        activate: bool = False,
        actor: Literal["human", "ai", "system"] = "human",
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        if activate:
            self._assert_no_running_target(session_id, thread_id)
        imported = self._configuration_registry.import_configuration(
            session_id,
            configuration,
            thread_id=thread_id,
            activate=activate,
        )
        if activate:
            self._session_state.sync_active_configuration(session_id, thread_id)
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "import_configuration",
            f"已导入调试方案 {imported.name}",
            actor=actor,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def copy_configuration(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        configuration_id: str,
        source_thread_id: str,
        target_thread_id: str,
        name: str | None = None,
        activate: bool = False,
    ) -> NodeDebugConfigurationDTO:
        source_session_id, source_thread_id = await self._admit_mutation(
            source_session_id, source_thread_id
        )
        target_session_id, target_thread_id = await self._admit_mutation(
            target_session_id, target_thread_id
        )
        self._session_state.ensure_loaded(target_session_id, target_thread_id)
        self.get_configuration(
            source_session_id, configuration_id, source_thread_id
        )
        if activate:
            self._assert_no_running_target(target_session_id, target_thread_id)
        copied = self._configuration_registry.copy_configuration(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            configuration_id=configuration_id,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            name=name,
            activate=activate,
        )
        if activate:
            self._session_state.sync_active_configuration(
                target_session_id, target_thread_id
            )
            self._drop_inactive_runtime((target_session_id, target_thread_id))
        self._record_session_action(
            target_session_id,
            "copy_configuration",
            f"已从会话 {source_session_id} 复制调试方案 {copied.name}",
            actor="human",
            thread_id=target_thread_id,
        )
        self._session_state.write_session_manifest(
            target_session_id, target_thread_id
        )
        return copied.model_copy(deep=True)

    async def start(
        self,
        *,
        session_id: str,
        path: str,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        thread_id: str,
        configuration_id: str | None = None,
        launch_profile_name: str | None = None,
        working_directory: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        """公开启动入口：Session 准入后，按 owner 串行执行整段启动序列。

        per-owner 临界区覆盖"核实旧登记 → durable 登记 → spawn → 身份核对 → 握手 → running"。
        并发 start（Web 与 Agent 同时按下）否则会各自登记一代 claim 并各自 spawn 进程：
        后写的登记会覆盖先启动实例的登记，先启动的进程随之游离，Inspector 端口也被抢。
        R5b 起同一临界区也覆盖 ``apply_action("stop")``、``restart()`` 与 ``close()`` 的
        停止路径：stop 落在 spawn 窗口不会再产生"exited + 进程存活"的假终态。临界区内
        的 await 全部有界（旧实例停止的 terminate/kill 核实各有 3s 超时、socket.close
        与已 cancel 任务的 gather 均有界），不会与 ``_runtimes_lock`` 形成反向持锁
        （``_owner_lock → _runtimes_lock → runtime.state_lock`` 单向）。
        """
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
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
                )
            )
            return await self.get_state(*result.owner)

    def _owner_lock(self, owner: NodeDebugOwner) -> asyncio.Lock:
        """取（或懒建）该 owner 的临界区：串行化 start/stop/restart/close 的整段序列。

        asyncio 单线程事件循环内 check-then-set 之间没有 await，不存在竞态。
        临界区内的 await 全部有界：旧实例停止的 terminate 核实 3s + kill 核实 3s
        超时、socket.close、已 cancel 任务的 gather；不存在无界等待，也不会与
        ``_runtimes_lock`` 形成反向持锁（``close()`` 先释放 ``_runtimes_lock`` 再取本锁）。
        """
        lock = self._owner_locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._owner_locks[owner] = lock
        return lock

    async def apply_action(
        self,
        *,
        command: NodeDebugActionRequest,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = command.session_id, command.thread_id
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
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
                return await self.get_state(session_id, thread_id)
            # stop 与 start 共用 per-owner 临界区（R3b 复核残留项）：否则 stop 落在
            # "runtime 已登记、进程尚未 spawn"的窗口会以 process is None 判定
            # "已停止/可证明不存在"，随后启动序列仍会 spawn，形成"exited + 进程
            # 存活"的假终态。临界区内的 await 全部有界（见 _owner_lock 文档）。
            async with self._owner_lock(owner):
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
                return await self.get_state(session_id, thread_id)
        self._session_state.persist_runtime_state(
            session_id, thread_id, runtime
        )
        return await self.get_state(session_id, thread_id)

    async def restart(
        self,
        session_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
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
                )
            )
            return await self.get_state(*result.owner)

    async def clear_all_breakpoints(
        self,
        session_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
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
            self._session_state.persist_runtime_state(
                session_id, thread_id, None
            )
            return await self.get_state(session_id, thread_id)
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
        self._session_state.persist_runtime_state(
            session_id, thread_id, runtime
        )
        return await self.get_state(session_id, thread_id)

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

    async def get_variables(
        self,
        *,
        session_id: str,
        thread_id: str,
        variable_names: list[str] | None = None,
        scope: str = "all",
    ) -> list[NodeDebugVariableDTO]:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        state = await self.get_state(session_id, thread_id)
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

    async def close(self) -> None:
        async with self._runtimes_lock:
            runtimes = tuple(self._runtimes.values())
        for runtime in runtimes:
            # 关停也走 per-owner 临界区：与并发 start/stop 串行化，避免关闭期间
            # 仍有启动序列在为同一 owner spawn 新进程。先释放 _runtimes_lock 再取
            # owner 锁，保持"_owner_lock → _runtimes_lock"的单向锁序。
            async with self._owner_lock((runtime.session_id, runtime.thread_id)):
                await self._lifecycle.stop_runtime(runtime)

    async def drain_session(self, session_id: str) -> None:
        """删除物理隔离前排空该 Session 的精确 main 调试 owner。

        SessionSubtreeDeleteService 已在对应 SessionLifecycleGate exclusive
        临界区内调用本方法。这里不走普通 mutation admission（catalog 已经
        原子进入 ``deleting``），而是直接按稳定 Session ID 取 main owner，
        串行停止在册 runtime，再核实并结清同一 owner 的 durable launch
        claim。任何 ``reconcile_required`` 或残留 active claim 都向上抛错，
        让共享删除流保持源目录和 deleting record，禁止伪成功。

        child Session 会以自己的 Session ID 作为冻结子树中的独立节点再次
        回调，因此这里不扫描父 Session 的 thread 目录，也不凭 PID/端口猜
        测其它 owner。
        """
        owner = self._owner_key(session_id, "main")
        async with self._owner_lock(owner):
            runtime = self._runtimes.get(owner)
            if runtime is not None:
                outcome = await self._lifecycle.stop_runtime(runtime, clear_error=False)
                if outcome == "reconcile_required":
                    raise RuntimeError(
                        "删除 Session 前无法核实 Node 调试进程终态，"
                        "保持 reconcile_required 并阻断删除: "
                        f"session_id={owner[0]}, thread_id={owner[1]}"
                    )
                async with runtime.state_lock:
                    runtime.status = "exited"
                    runtime.error_message = None
                # 与 Web/API stop 入口保持同一 authoritative manifest 语义；
                # 物理 rename 发生在本回调返回之后。
                self._session_state.persist_runtime_state(
                    owner[0], owner[1], runtime
                )

            # runtime 缺失时按 durable claim 恢复合同定点核实旧实例；若
            # claim 仍不可核实，必须阻断删除，而不能把内存缺项当成 stopped。
            decision = await self._lifecycle.reconcile_persisted_claim(owner)
            if decision is not None and decision.outcome == "reconcile_required":
                raise RuntimeError(
                    "删除 Session 前无法核实 Node 调试 claim，"
                    "保持 reconcile_required 并阻断删除: "
                    f"session_id={owner[0]}, thread_id={owner[1]}, "
                    f"reason={decision.reason}"
                )
            claim = self._claim_runtime.active_claim(*owner)
            if claim is not None:
                raise RuntimeError(
                    "删除 Session 前仍存在未结清的 Node 调试 claim，"
                    "阻断物理隔离: "
                    f"session_id={owner[0]}, thread_id={owner[1]}, "
                    f"phase={claim.phase}"
                )

    @staticmethod
    def _owner_key(session_id: str, thread_id: str) -> NodeDebugOwner:
        """纯 owner key 归一，只用于精确 owner 参数，不触碰目录索引。"""
        return normalize_node_debug_owner(session_id, thread_id)

    def _resolve_owner(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugOwner:
        """读入口的 owner 归一：有目录索引时按受检 thread 节点折叠别名地址。"""
        if self._thread_path_resolver is None:
            return self._owner_key(session_id, thread_id)
        return resolve_node_debug_owner(
            self._thread_path_resolver,
            session_id=session_id,
            thread_id=thread_id,
        ).key

    async def _admit_mutation(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugOwner:
        """调试 mutation 的 Session 生命周期准入，并返回受检的精确 owner。

        准入同时收口持久 launch claim：能核实旧实例已终结的当场结清，无法核实的保持
        ``reconcile_required`` 并在后续断言中阻断。
        """
        owner = (await self._session_admission.admit(session_id, thread_id)).key
        await self._lifecycle.reconcile_persisted_claim(owner)
        return owner

    def residency_blockers(
        self, session_id: str, thread_id: str
    ) -> list[ResidencyBlocker]:
        """从 claim/runtime 投影读取该 owner 当前仍活跃的占用。"""
        owner = self._owner_key(session_id, thread_id)
        return self._claim_runtime.residency_blockers(*owner)

    def _record_session_action(
        self,
        session_id: str,
        action: str,
        message: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is not None:
            self._append_action(
                runtime,
                action,
                message,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._session_state.set_pending_actions(
                owner,
                runtime.actions[-MAX_NODE_DEBUG_ACTIONS:],
            )
            return
        self._session_state.append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

    def _drop_inactive_runtime(self, owner: NodeDebugOwner) -> None:
        runtime = self._runtimes.get(owner)
        if runtime is not None and runtime.status not in {
            "starting",
            "running",
            "paused",
        }:
            self._runtimes.pop(owner, None)

    def _assert_no_running_target(self, session_id: str, thread_id: str) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is not None and runtime.status in {
            "starting",
            "running",
            "paused",
            "stopping",
            "reconcile_required",
        }:
            raise RuntimeError("目标程序运行中，停止后才能切换调试方案")
        self._assert_no_unsettled_claim(owner, operation="切换调试方案")

    def _assert_configuration_not_running(
        self,
        session_id: str,
        thread_id: str,
        configuration_id: str,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if (
            runtime is not None
            and runtime.configuration_id == configuration_id
            and runtime.status in {
                "starting",
                "running",
                "paused",
                "stopping",
                "reconcile_required",
            }
        ):
            raise RuntimeError("目标程序运行中，不能修改或删除当前调试方案")
        # 与 _assert_no_running_target 的阻面对齐（R3b 建议 3）：冷场景（backend
        # 重启后无在册 runtime）下，未结清的 durable claim（含 reconcile_required）
        # 同样必须阻断方案修改/删除。
        self._assert_no_unsettled_claim(owner, operation="修改或删除当前调试方案")

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

    async def _read_stream(
        self,
        runtime: NodeDebugRuntime,
        stream_name: str,
    ) -> None:
        process = runtime.process
        if process is None:
            return
        stream = getattr(process, stream_name)
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            match = _INSPECTOR_URL_PATTERN.search(text)
            if match:
                runtime.inspector.inspector_url = match.group(1)
                runtime.inspector.inspector_ready.set()
                continue
            if stream_name == "stdout" and text:
                logpoint_error = parse_logpoint_error(text)
                if logpoint_error is not None:
                    text = f"[日志点错误] {logpoint_error}"
                    async with runtime.state_lock:
                        runtime.logpoint_error_message = (
                            f"日志点求值失败: {logpoint_error}"
                        )
                        runtime.error_message = runtime.logpoint_error_message
                        self._append_action(
                            runtime,
                            "logpoint_error",
                            runtime.logpoint_error_message,
                            actor="system",
                            result="error",
                        )
                else:
                    logpoint_output = parse_logpoint_output(text)
                    if logpoint_output is not None:
                        text = f"[日志点] {logpoint_output}"
                async with runtime.state_lock:
                    runtime.output.append(text)
                    del runtime.output[:-_MAX_OUTPUT_LINES]

    async def _monitor_process(self, runtime: NodeDebugRuntime) -> None:
        process = runtime.process
        if process is None:
            return
        return_code = await process.wait()
        # 进程句柄已报告终态：这是可核实的终结，结清本实例的 claim。
        self._claim_runtime.mark_claim_phase(
            runtime, "settled", f"进程已退出，退出码: {return_code}"
        )
        if runtime.closing:
            return
        async with runtime.state_lock:
            runtime.status = "exited" if return_code == 0 else "failed"
            self._clear_stop_snapshot(runtime)
            runtime.error_message = (
                runtime.logpoint_error_message
                if runtime.logpoint_error_message is not None
                else (
                    None
                    if return_code == 0
                    else f"Node 调试进程退出，退出码: {return_code}"
                )
            )

    @staticmethod
    def _paused_at_breakpoint(runtime: NodeDebugRuntime) -> bool:
        if runtime.paused_breakpoint_ids:
            return bool(
                runtime.paused_breakpoint_ids
                & set(runtime.inspector_breakpoint_ids.values())
            )
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

    def _clear_stop_snapshot(self, runtime: NodeDebugRuntime) -> None:
        self._inspector.clear_paused_snapshot(runtime)
        runtime.inspector_breakpoint_ids.clear()
        runtime.breakpoints = {
            breakpoint_id: breakpoint.model_copy(
                update={
                    "verified": False,
                    "actual_line": None,
                    "inspector_id": None,
                }
            )
            for breakpoint_id, breakpoint in runtime.breakpoints.items()
        }

    @staticmethod
    def _append_action(
        runtime: NodeDebugRuntime,
        action: str,
        message: str,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
        result: Literal["success", "error"] = "success",
    ) -> None:
        append_runtime_debug_action(
            runtime,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            extension_catalog_binding=extension_catalog_binding,
            result=result,
            max_actions=MAX_NODE_DEBUG_ACTIONS,
        )

    def _snapshot(self, runtime: NodeDebugRuntime) -> NodeDebugStateDTO:
        return build_node_debug_snapshot(
            runtime,
            self._configuration_registry,
        )
