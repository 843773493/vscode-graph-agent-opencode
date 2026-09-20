from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import unquote, urlparse

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from app.core.identifier import create_prefixed_id
from app.core.path_utils import safe_join
from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugAction,
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugBreakpointRequest,
    NodeDebugCapabilitiesDTO,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugEvaluationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugLaunchProfileDTO,
    NodeDebugSessionManifestDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
    NodeDebugStatus,
    NodeDebugVariableDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.node_debug_breakpoint_expressions import (
    inspector_breakpoint_condition,
    parse_logpoint_error,
    parse_logpoint_output,
)
from app.services.infrastructure.node_debug_breakpoints import (
    anchor_breakpoint,
    persistable_breakpoint,
    portable_breakpoint,
    reconcile_breakpoint,
    runtime_breakpoint,
    source_digest,
)
from app.services.infrastructure.node_debug_configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug_launch_claim import (
    ACTIVE_CLAIM_PHASES,
    NodeDebugClaimRecoveryDecision,
    claim_marked,
    claim_running,
    claim_with_spawn_identity,
    decide_claim_recovery,
    new_launch_claim,
)
from app.services.infrastructure.node_debug_process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug_session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug_thread_owner import (
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
from app.services.infrastructure.node_debug_session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug_snapshot import (
    append_pending_debug_action,
    append_runtime_debug_action,
    build_node_debug_snapshot,
)

_INSPECTOR_URL_PATTERN = re.compile(r"Debugger listening on (ws://\S+)")

logger = logging.getLogger(__name__)
_SUPPORTED_EXTENSIONS = {".cjs", ".js", ".mjs"}
_MAX_ACTIONS = 100
_MAX_OUTPUT_LINES = 100
_COMMAND_TIMEOUT_SECONDS = 10.0
_TERMINATE_TIMEOUT_SECONDS = 3.0
_KILL_TIMEOUT_SECONDS = 3.0
_RECONCILE_TERMINATE_TIMEOUT_SECONDS = 5.0
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
#: ThreadResidency idle blocker 的脱敏话术（按已核实的 claim 相位固定）。
#: 绝不携带 PID/端口/路径/process_instance_id 正文，也不拷贝 claim.reconcile_reason
#:（其中含诊断明细）；residency 快照对前端只暴露类别与这里的固定话术。
_NODE_DEBUG_BLOCKER_REASON: dict[str, str] = {
    "launch_pending": "Node 调试进程已登记启动，等待 spawn 与握手核实",
    "spawned": "Node 调试进程已启动，等待 Inspector 握手核实",
    "running": "Node 调试进程运行中",
    "stopping": "Node 调试进程停止中，等待核实终结",
    "reconcile_required": "Node 调试实例无法核实终态，需核实后才能解除占用",
}
#: 在册 runtime 的活跃状态（pull 源的安全网：claim 缺失时也绝不虚报可卸载）。
_RESIDENCY_ACTIVE_RUNTIME_STATUSES = frozenset(
    {"starting", "running", "paused", "stopping", "reconcile_required"}
)


@dataclass(slots=True)
class _NodeDebugRuntime:
    session_id: str
    thread_id: str
    configuration_id: str
    workspace_root: Path
    script_path: Path
    relative_script_path: str
    args: list[str] = field(default_factory=list)
    working_directory: Path | None = None
    launch_profile_name: str | None = None
    node_bin: str | None = None
    inspector_host: str = "127.0.0.1"
    inspector_port: int = 0
    command_timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS
    process: asyncio.subprocess.Process | None = None
    socket: ClientConnection | None = None
    inspector_url: str | None = None
    status: NodeDebugStatus = "starting"
    #: 本次启动唯一的 process instance 身份；旧 generation 的回调不得写新实例。
    process_instance_id: str | None = None
    process_identity_source: str | None = None
    process_start_marker: str | None = None
    paused_reason: str | None = None
    paused_breakpoint_ids: set[str] = field(default_factory=set)
    error_message: str | None = None
    call_stack: list[NodeDebugStackFrameDTO] = field(default_factory=list)
    last_stopped_frame: NodeDebugStackFrameDTO | None = None
    scope_object_ids: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    script_urls: dict[str, str] = field(default_factory=dict)
    breakpoints: dict[str, NodeDebugBreakpointDTO] = field(default_factory=dict)
    inspector_breakpoint_ids: dict[str, str] = field(default_factory=dict)
    output: list[str] = field(default_factory=list)
    logpoint_error_message: str | None = None
    last_evaluation: NodeDebugEvaluationDTO | None = None
    evaluations: list[NodeDebugEvaluationDTO] = field(default_factory=list)
    actions: list[NodeDebugActionRecordDTO] = field(default_factory=list)
    next_command_id: int = 1
    pending_commands: dict[int, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict
    )
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inspector_ready: asyncio.Event = field(default_factory=asyncio.Event)
    receiver_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    stdout_task: asyncio.Task[None] | None = None
    process_task: asyncio.Task[None] | None = None
    variable_hydration_task: asyncio.Task[None] | None = None
    loaded_source_digests: dict[str, str | None] = field(default_factory=dict)
    requires_restart: bool = False
    source_changed_paths: set[str] = field(default_factory=set)
    closing: bool = False


@dataclass(slots=True)
class _NodeDebugLaunchSelection:
    script_path: str | None = None
    working_directory: str | None = None
    launch_profile_name: str | None = None
    args: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NodeDebugProcessLeaseIdentity:
    """typed ``node_debug_process`` 在唯一 external_resource_leases 账本中的身份。

    由精确 debug owner 与本次启动唯一的 ``process_instance_id`` 派生：每次启动一个
    占用 lease，holder 是 debug owner 本身，而不是发起本次操作的 tool_call / Web
    request 或其短期 operation lease。``holder_id`` 写入 lease 记录的
    ``turn_stream_id`` 字段（该字段在本账本中表达 holder identity），取值带专用
    前缀，不会与真实 turn stream id 相等，因此 ``release_turn_leases`` 的 Turn 收尾
    绝不会误释放跨 Turn 的调试进程占用。
    """

    resource_id: str
    holder_id: str
    lease_id: str
    operation_id: str

    @classmethod
    def for_process_instance(
        cls,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> NodeDebugProcessLeaseIdentity:
        resource_id = f"node_debug_process:{session_id}:{thread_id}"
        return cls(
            resource_id=resource_id,
            holder_id=f"node-debug-owner:{session_id}:{thread_id}",
            lease_id=f"{resource_id}:{process_instance_id}",
            operation_id=process_instance_id,
        )


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
        #: 占用，不参与任何进程状态判断（见 _ensure_process_lease 的职责说明）。
        self._external_resource_leases = external_resource_leases
        #: ThreadResidency 的 blocker 上报目标（R5b）：把已核实的 claim 相位变化单向
        #: 推送为 thread 的 idle blocker；本服务绝不反向读取 residency 推断进程状态。
        self._residency_tracker = residency_tracker
        #: resource.state/{owner_domain} 轻量通知出口：只在 owner 已核实的
        #: 释放失败（进入 reconcile_required）时发布 release_failed；成功终态
        #: 由账本 settle 发布 released。通知失败不改变 durable claim 事实。
        self._state_events = state_events
        self._runtimes: dict[NodeDebugOwner, _NodeDebugRuntime] = {}
        #: per-owner 启动临界区：串行化“核实旧 claim → durable 登记 → spawn → 握手”。
        #: 条目数与 ``_runtimes`` 同量级（每个被触达过的 owner 一把锁）；不做回收，
        #: 以免丢弃仍被并发任务持有的锁。
        self._owner_locks: dict[NodeDebugOwner, asyncio.Lock] = {}
        self._pending_breakpoints: dict[NodeDebugOwner, list[NodeDebugBreakpointDTO]] = {}
        self._pending_actions: dict[NodeDebugOwner, list[NodeDebugActionRecordDTO]] = {}
        self._launch_selections: dict[NodeDebugOwner, _NodeDebugLaunchSelection] = {}
        self._configuration_registry = NodeDebugConfigurationRegistry(
            store=session_store,
            validate_configuration=self._validate_configuration,
        )
        self._session_admission = session_admission
        # 入口别名折叠需要目录索引；无持久化会话树场景（嵌入式/单测）没有可折叠的
        # thread 节点，只做 owner 归一。
        self._thread_path_resolver = (
            session_store.path_resolver if session_store is not None else None
        )
        self._runtimes_lock = asyncio.Lock()
        self._node_bin = os.environ.get("BOXTEAM_NODE_BIN") or shutil.which("node")

    async def get_state(
        self, session_id: str, thread_id: str
    ) -> NodeDebugStateDTO:
        owner = self._resolve_owner(session_id, thread_id)
        session_id, thread_id = owner
        self._ensure_session_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            # 冷读取时必须先按持久 claim 核实旧实例，绝不虚报 idle/终态。
            await self._reconcile_persisted_claim(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
        if runtime is None:
            selection = self._launch_selections.get(
                owner,
                _NodeDebugLaunchSelection(),
            )
            claim = self._active_claim(session_id, thread_id)
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
                    for breakpoint in self._pending_breakpoints.get(owner, [])
                ],
                actions=[
                    action.model_copy(deep=True)
                    for action in self._pending_actions.get(owner, [])
                ],
                configuration_revision=(
                    self._configuration_registry.active_revision(session_id, thread_id)
                ),
            )
        async with runtime.state_lock:
            return self._snapshot(runtime)

    def get_capabilities(self) -> NodeDebugCapabilitiesDTO:
        """返回供客户端选择启动配置的脱敏调试能力。"""
        debug_config = self._get_debug_runtime_config()
        raw_profiles = debug_config.get("launch_profiles")
        if not isinstance(raw_profiles, dict):
            raise TypeError("runtime.debug.launch_profiles 配置无效")
        profiles: list[NodeDebugLaunchProfileDTO] = []
        for name, raw_profile in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(raw_profile, dict):
                raise TypeError("runtime.debug.launch_profiles 配置无效")
            adapter = raw_profile.get("adapter")
            runtime = raw_profile.get("runtime")
            program = raw_profile.get("program", "")
            working_directory = raw_profile.get("working_directory", "")
            args = raw_profile.get("args", [])
            if (
                not isinstance(adapter, str)
                or not isinstance(runtime, str)
                or not isinstance(program, str)
                or not isinstance(working_directory, str)
                or not isinstance(args, list)
                or not all(isinstance(argument, str) for argument in args)
            ):
                raise TypeError(f"runtime.debug.launch_profiles.{name} 规范化结果无效")
            profiles.append(
                NodeDebugLaunchProfileDTO(
                    name=name,
                    adapter=adapter,
                    runtime=runtime,
                    supported=adapter == "node_inspector" and runtime == "node",
                    program=program,
                    working_directory=working_directory,
                    args=list(args),
                )
            )
        default_adapter = debug_config.get("default_adapter")
        enabled = debug_config.get("enabled")
        if not isinstance(default_adapter, str) or not isinstance(enabled, bool):
            raise TypeError("runtime.debug 规范化结果无效")
        return NodeDebugCapabilitiesDTO(
            enabled=enabled,
            default_adapter=default_adapter,
            supported_adapters=["node_inspector"],
            launch_profiles=profiles,
        )

    def resolve_launch_profile_name(self, launch_profile_name: str | None) -> str:
        """把方案/请求里的 profile 名称解析为实际生效的 profile 名称。

        Agent 工具面需要在启动前核对“显式 profile 与方案解析结果一致”，
        因此复用唯一的 ``_resolve_launch_profile`` 解析规则，避免在工具层
        复制默认 profile 名称形成第二套语义。本方法只读配置，不触碰运行时。
        """
        resolved_name, _ = self._resolve_launch_profile(
            self._get_debug_runtime_config(),
            launch_profile_name,
        )
        return resolved_name

    def list_configurations(
        self,
        session_id: str,
        thread_id: str,
    ) -> list[NodeDebugConfigurationDTO]:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._ensure_session_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration_registry.list(session_id, thread_id)

    def get_configuration(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._ensure_session_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration(
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
        self._ensure_session_loaded(session_id, thread_id)
        self._configuration_registry.assert_unique_name(
            session_id,
            request.name,
            thread_id=thread_id,
        )
        if request.activate:
            self._assert_no_running_target(session_id, thread_id)
        configuration = self._configuration_from_request(
            configuration_id=create_prefixed_id("dbgcfg"),
            name=request.name,
            script_path=request.script_path,
            working_directory=request.working_directory,
            launch_profile_name=request.launch_profile_name,
            args=request.args,
            breakpoints=request.breakpoints,
        )
        self._configuration_registry.put(session_id, configuration, thread_id)
        if request.activate:
            self._activate_configuration_in_memory(
                session_id,
                thread_id,
                configuration.configuration_id,
            )
        self._record_session_action(
            session_id,
            "create_configuration",
            f"已创建调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._write_session_manifest(session_id, thread_id)
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
        self._ensure_session_loaded(session_id, thread_id)
        current = self._configuration(session_id, thread_id, configuration_id)
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        self._configuration_registry.assert_unique_name(
            session_id,
            request.name,
            thread_id=thread_id,
            exclude_configuration_id=configuration_id,
        )
        replacement = self._configuration_from_request(
            configuration_id=configuration_id,
            name=request.name,
            script_path=request.script_path,
            working_directory=request.working_directory,
            launch_profile_name=request.launch_profile_name,
            args=request.args,
            breakpoints=request.breakpoints,
            revision=current.revision + 1,
            created_at=current.created_at,
        )
        self._configuration_registry.put(session_id, replacement, thread_id)
        if self._configuration_registry.active_id(session_id, thread_id) == configuration_id:
            self._activate_configuration_in_memory(
                session_id, thread_id, configuration_id
            )
        self._record_session_action(
            session_id,
            "update_configuration",
            f"已更新调试方案 {replacement.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._write_session_manifest(session_id, thread_id)
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
        self._ensure_session_loaded(session_id, thread_id)
        configuration = self._configuration(session_id, thread_id, configuration_id)
        self._assert_no_running_target(session_id, thread_id)
        self._activate_configuration_in_memory(
            session_id, thread_id, configuration_id
        )
        self._record_session_action(
            session_id,
            "activate_configuration",
            f"已激活调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._write_session_manifest(session_id, thread_id)
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
        owner = self._owner_key(session_id, thread_id)
        self._ensure_session_loaded(session_id, thread_id)
        configuration = self._configuration(session_id, thread_id, configuration_id)
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        self._configuration_registry.remove(session_id, configuration_id, thread_id)
        if self._configuration_registry.active_id(session_id, thread_id) == configuration_id:
            self._configuration_registry.clear_active(session_id, thread_id)
            self._launch_selections.pop(owner, None)
            self._pending_breakpoints.pop(owner, None)
            self._runtimes.pop(owner, None)
        self._record_session_action(
            session_id,
            "delete_configuration",
            f"已删除调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._write_session_manifest(session_id, thread_id)
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
        self._ensure_session_loaded(session_id, thread_id)
        if activate:
            self._assert_no_running_target(session_id, thread_id)
        if self._configuration_registry.contains(
            session_id,
            configuration.configuration_id,
            thread_id,
        ):
            raise ValueError(
                f"目标会话已存在调试方案: {configuration.configuration_id}"
            )
        self._configuration_registry.assert_unique_name(
            session_id,
            configuration.name,
            thread_id=thread_id,
        )
        imported = self._validate_configuration(configuration)
        self._configuration_registry.put(session_id, imported, thread_id)
        if activate:
            self._assert_no_running_target(session_id, thread_id)
            self._activate_configuration_in_memory(
                session_id,
                thread_id,
                imported.configuration_id,
            )
        self._record_session_action(
            session_id,
            "import_configuration",
            f"已导入调试方案 {imported.name}",
            actor=actor,
            thread_id=thread_id,
        )
        self._write_session_manifest(session_id, thread_id)
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
        source = self.get_configuration(
            source_session_id, configuration_id, source_thread_id
        )
        self._ensure_session_loaded(target_session_id, target_thread_id)
        if activate:
            self._assert_no_running_target(target_session_id, target_thread_id)
        target_name = (name or source.name).strip()
        self._configuration_registry.assert_unique_name(
            target_session_id,
            target_name,
            thread_id=target_thread_id,
        )
        now = datetime.now(UTC)
        copied = self._validate_configuration(
            source.model_copy(
                update={
                    "configuration_id": create_prefixed_id("dbgcfg"),
                    "name": target_name,
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                },
                deep=True,
            )
        )
        self._configuration_registry.put(target_session_id, copied, target_thread_id)
        if activate:
            self._assert_no_running_target(target_session_id, target_thread_id)
            self._activate_configuration_in_memory(
                target_session_id,
                target_thread_id,
                copied.configuration_id,
            )
        self._record_session_action(
            target_session_id,
            "copy_configuration",
            f"已从会话 {source_session_id} 复制调试方案 {copied.name}",
            actor="human",
            thread_id=target_thread_id,
        )
        self._write_session_manifest(target_session_id, target_thread_id)
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
            return await self._launch_under_claim_gate(
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

    async def _launch_under_claim_gate(
        self,
        *,
        owner: NodeDebugOwner,
        path: str,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        configuration_id: str | None,
        launch_profile_name: str | None,
        working_directory: str | None,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> NodeDebugStateDTO:
        """已在 owner 临界区内的启动主体；``owner`` 是入口归一/折叠后的精确归属。"""
        session_id, thread_id = owner
        self._ensure_session_loaded(session_id, thread_id)
        await self._assert_claim_recoverable(owner)
        await self._reconcile_session_sources(
            session_id,
            thread_id,
            self._runtimes.get(owner),
        )
        selected_configuration_id = self._select_configuration_for_start(
            session_id=session_id,
            thread_id=thread_id,
            configuration_id=configuration_id,
            path=path,
            working_directory=working_directory,
            launch_profile_name=launch_profile_name,
            args=args,
        )
        selected_configuration = self._configuration(
            session_id,
            thread_id,
            selected_configuration_id,
        )
        if selected_configuration.script_path is None:
            raise ValueError(f"调试方案没有目标文件: {selected_configuration.name}")
        # 一旦会话存在活动方案，方案文件就是启动参数的唯一权威来源。
        # Web 或 Agent 若要改变入口、工作目录、profile 或参数，必须先显式保存方案。
        path = selected_configuration.script_path
        working_directory = selected_configuration.working_directory
        launch_profile_name = selected_configuration.launch_profile_name
        args = list(selected_configuration.args)
        debug_config = self._get_debug_runtime_config()
        if not debug_config["enabled"]:
            raise RuntimeError("源码调试能力未启用: runtime.debug.enabled=false")
        profile_name, profile = self._resolve_launch_profile(
            debug_config,
            launch_profile_name,
        )
        adapter = profile["adapter"]
        if adapter != "node_inspector":
            raise RuntimeError(
                f"当前版本不支持调试 adapter: {adapter}; 仅支持 node_inspector"
            )
        if profile["runtime"] != "node":
            raise RuntimeError(
                f"Node Inspector profile 的 runtime 必须是 node: {profile['runtime']!r}"
            )
        resolved_working_directory = self._resolve_working_directory(
            working_directory or profile["working_directory"]
        )
        script_path, relative_path = self._resolve_script_path(path)
        normalized_args = self._normalize_args(args if args else profile["args"])
        node_config = debug_config["node"]
        configured_node_bin = node_config["executable"].strip()
        node_bin = configured_node_bin or self._node_bin
        if not node_bin:
            raise RuntimeError(
                "未找到 Node.js，可通过 runtime.debug.node.executable 或 "
                "BOXTEAM_NODE_BIN 指定"
            )
        pending_breakpoints = list(self._pending_breakpoints.get(owner, []))
        pending_actions = list(self._pending_actions.get(owner, []))
        async with self._runtimes_lock:
            previous = self._runtimes.get(owner)
            previous_breakpoints: list[NodeDebugBreakpointDTO] = []
            previous_actions: list[NodeDebugActionRecordDTO] = []
            if previous is not None:
                async with previous.state_lock:
                    previous_breakpoints = [
                        breakpoint.model_copy(deep=True)
                        for breakpoint in previous.breakpoints.values()
                    ]
                    previous_actions = [
                        action.model_copy(deep=True)
                        for action in previous.actions[-_MAX_ACTIONS:]
                    ]
            if previous is not None and previous.status in {
                "starting",
                "running",
                "paused",
                "stopping",
                "reconcile_required",
            }:
                previous_outcome = await self._stop_runtime(previous)
                if previous_outcome == "reconcile_required":
                    raise RuntimeError(
                        "旧调试实例无法核实终态，已保持 reconcile_required；"
                        "核实并结清前拒绝启动新实例: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
            runtime = _NodeDebugRuntime(
                session_id=session_id,
                thread_id=thread_id,
                configuration_id=selected_configuration_id,
                workspace_root=self._workspace_root,
                script_path=script_path,
                relative_script_path=relative_path,
                args=list(normalized_args),
                working_directory=resolved_working_directory,
                launch_profile_name=profile_name,
                node_bin=node_bin,
                inspector_host=node_config["inspector_host"],
                inspector_port=node_config["inspector_port"],
                command_timeout_seconds=debug_config["command_timeout_seconds"],
            )
            requested_breakpoints = [
                breakpoint.model_copy(deep=True) for breakpoint in pending_breakpoints
            ]
            if (
                previous is not None
                and previous.configuration_id == selected_configuration_id
            ):
                requested_breakpoints.extend(previous_breakpoints)
            requested_breakpoints.extend(
                self._create_breakpoint(
                    path=breakpoint.path,
                    line=breakpoint.line,
                    column=breakpoint.column,
                    condition=breakpoint.condition,
                    hit_condition=breakpoint.hit_condition,
                    log_message=breakpoint.log_message,
                )
                for breakpoint in breakpoints
            )
            unique_breakpoints = {
                (
                    breakpoint.path,
                    breakpoint.line,
                    breakpoint.column,
                ): breakpoint
                for breakpoint in requested_breakpoints
            }
            for requested_breakpoint in unique_breakpoints.values():
                breakpoint = requested_breakpoint.model_copy(
                    update={
                        "verified": False,
                        "actual_line": None,
                        "inspector_id": None,
                    }
                )
                runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint
            source_actions = (
                previous_actions if previous is not None else pending_actions
            )
            runtime.actions.extend(
                action.model_copy(deep=True) for action in source_actions
            )
            del runtime.actions[:-_MAX_ACTIONS]
            self._runtimes[owner] = runtime
            self._pending_breakpoints.pop(owner, None)
            self._pending_actions.pop(owner, None)
            self._launch_selections[owner] = _NodeDebugLaunchSelection(
                script_path=relative_path,
                working_directory=(
                    resolved_working_directory.relative_to(
                        self._workspace_root
                    ).as_posix()
                    if resolved_working_directory != self._workspace_root
                    else ""
                ),
                launch_profile_name=profile_name,
                args=list(normalized_args),
            )
            runtime.loaded_source_digests = self._source_digests_for_runtime(runtime)
            self._persist_session_state(session_id, thread_id, runtime)

        # spawn 前先 durable 登记唯一 process_instance_id + nonce；该登记独立于本次
        # 调用，跨 Turn 保留，崩溃后可据此定点恢复。
        claim = new_launch_claim(
            session_id=session_id,
            thread_id=thread_id,
            configuration_id=selected_configuration_id,
            inspector_host=runtime.inspector_host,
            inspector_port=runtime.inspector_port,
        )
        runtime.process_instance_id = claim.process_instance_id
        self._write_launch_claim(claim)
        try:
            # spawn 前的 closing 守卫（R3b 复核残留项）：stop 已把该 runtime 置为
            # closing（停止序列已接管，可能已按"进程尚未 spawn"核实终结并置 exited）
            # 时，启动序列绝不能再 spawn 出游离进程，否则会留下"内存态 exited +
            # 进程存活 + claim running"的假终态。守卫抛错走下方统一的失败收口
            # （状态 failed、claim 结清），绝不虚报启动成功。
            async with runtime.state_lock:
                if runtime.closing:
                    raise RuntimeError(
                        "并发的停止请求已接管该调试运行时，取消 spawn: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
            # spawn 必须在登记之后、且在统一的失败收口之内：`create_subprocess_exec`
            # 抛错时 `runtime.process` 仍为 None，`_terminate_and_verify` 据此可证明
            # 没有产生任何进程实例，从而把 claim 结清；否则该 owner 会留下永远无法
            # 核实的 launch_pending 登记，把后续启动全部错误阻断。
            runtime.process = await asyncio.create_subprocess_exec(
                node_bin,
                f"--inspect-brk={runtime.inspector_host}:{runtime.inspector_port}",
                str(script_path),
                *normalized_args,
                cwd=str(resolved_working_directory),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            runtime.stderr_task = asyncio.create_task(self._read_stream(runtime, "stderr"))
            runtime.stdout_task = asyncio.create_task(self._read_stream(runtime, "stdout"))
            runtime.process_task = asyncio.create_task(self._monitor_process(runtime))
            # spawn 后立刻核对 OS 进程起始身份；PID 会被复用，绝不能只凭 PID 认领。
            spawn_identity = probe_process_identity(runtime.process.pid)
            if spawn_identity is None:
                raise RuntimeError(
                    "spawn 后无法读取进程起始身份（进程可能已立即退出）: "
                    f"pid={runtime.process.pid}"
                )
            runtime.process_identity_source = spawn_identity.source
            runtime.process_start_marker = spawn_identity.start_marker
            self._write_launch_claim(
                claim_with_spawn_identity(
                    claim,
                    pid=runtime.process.pid,
                    identity=spawn_identity,
                )
            )
            await asyncio.wait_for(
                runtime.inspector_ready.wait(),
                timeout=runtime.command_timeout_seconds,
            )
            if runtime.inspector_url is None:
                raise RuntimeError("Node Inspector 已报告就绪，但缺少 WebSocket 地址")
            import websockets

            # Node Inspector 不兼容 websockets 默认的 20 秒 keepalive ping；
            # 该 ping 会导致连接关闭，进而让暂停中的脚本继续执行。
            runtime.socket = await websockets.connect(
                runtime.inspector_url,
                ping_interval=None,
            )
            runtime.receiver_task = asyncio.create_task(self._receive_messages(runtime))
            await self._command(runtime, "Runtime.enable")
            await self._command(runtime, "Debugger.enable")
            await self._command(
                runtime,
                "NodeRuntime.notifyWhenWaitingForDisconnect",
                {"enabled": True},
            )
            # 起始身份核对 + Inspector 握手都成功，才把 PID/端口登记为权威运行属性。
            self._mark_claim_running(runtime)
            for breakpoint in runtime.breakpoints.values():
                if breakpoint.relocation_status != "current":
                    continue
                await self._install_breakpoint(runtime, breakpoint)
            async with runtime.state_lock:
                runtime.status = "starting"
            await self._command(runtime, "Runtime.runIfWaitingForDebugger")
            await self._wait_for_execution_state(runtime)
            resume_initial_pause = False
            async with runtime.state_lock:
                if runtime.status == "paused" and not self._paused_at_breakpoint(
                    runtime
                ):
                    runtime.status = "running"
                    self._clear_paused_snapshot(runtime)
                    # `--inspect-brk` 的入口暂停不是用户设置的源码断点，不能让它
                    # 覆盖右侧调试预览所展示的最后一次真实停止位置。
                    runtime.last_stopped_frame = None
                    resume_initial_pause = True
            if resume_initial_pause:
                await self._command(runtime, "Debugger.resume")
                await self._wait_for_execution_state(runtime)
            await self._wait_for_frame_variables(runtime)
            async with runtime.state_lock:
                if runtime.status not in {"exited", "failed", "paused"} and (
                    runtime.process is None or runtime.process.returncode is None
                ):
                    runtime.status = "running"
                runtime.error_message = runtime.logpoint_error_message
                self._append_action(
                    runtime,
                    "start",
                    "已启动 Node Inspector",
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
            self._persist_session_state(session_id, thread_id, runtime)
        except Exception as error:
            message = f"启动 Node Inspector 失败: {error}"
            async with runtime.state_lock:
                runtime.status = "failed"
                runtime.error_message = message
                self._append_action(
                    runtime,
                    "start_failed",
                    message,
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result="error",
                )
            self._persist_session_state(session_id, thread_id, runtime)
            await self._stop_runtime(runtime, clear_error=False)
            raise RuntimeError(message) from error
        return await self.get_state(session_id, thread_id)

    async def apply_action(
        self,
        *,
        session_id: str,
        action: NodeDebugAction,
        params: dict[str, object],
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        self._ensure_session_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        await self._reconcile_session_sources(session_id, thread_id, runtime)
        if runtime is None and action == "set_breakpoint":
            self._ensure_configuration_for_breakpoint(session_id, thread_id, params)
        if runtime is None and action not in {
            "set_breakpoint",
            "update_breakpoint",
            "clear_breakpoint",
        }:
            self._assert_no_unsettled_claim(owner, operation=f"调试动作 {action}")
            raise RuntimeError(
                f"Node 调试会话不存在: session_id={session_id}, thread_id={thread_id}"
            )
        if action == "set_breakpoint":
            await self._set_breakpoint(
                owner,
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "update_breakpoint":
            await self._update_breakpoint(
                owner,
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "clear_breakpoint":
            await self._clear_breakpoint(
                owner,
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "evaluate":
            await self._evaluate(
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "stop":
            # stop 与 start 共用 per-owner 临界区（R3b 复核残留项）：否则 stop 落在
            # "runtime 已登记、进程尚未 spawn"的窗口会以 process is None 判定
            # "已停止/可证明不存在"，随后启动序列仍会 spawn，形成"exited + 进程
            # 存活"的假终态。临界区内的 await 全部有界（见 _owner_lock 文档）。
            async with self._owner_lock(owner):
                outcome = await self._stop_runtime(runtime)
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
                self._persist_session_state(session_id, thread_id, runtime)
                return await self.get_state(session_id, thread_id)
        else:
            await self._debugger_command(
                runtime,
                action,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        self._persist_session_state(session_id, thread_id, runtime)
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
        # 直接调 `_launch_under_claim_gate`，绝不能再经 `start()` 重入同一把
        # asyncio.Lock（不可重入，重入即自锁）。
        async with self._owner_lock(owner):
            self._ensure_session_loaded(session_id, thread_id)
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
            outcome = await self._stop_runtime(runtime)
            if outcome == "reconcile_required":
                # 旧实例无法核实终态：保持 reconcile_required，绝不为同一 owner 启动新实例。
                self._persist_session_state(session_id, thread_id, runtime)
                raise RuntimeError(
                    "旧调试实例无法核实终态，保持 reconcile_required；拒绝重启: "
                    f"session_id={session_id}, thread_id={thread_id}"
                )
            async with runtime.state_lock:
                runtime.status = "exited"
            return await self._launch_under_claim_gate(
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
        self._ensure_session_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            removed = len(self._pending_breakpoints.get(owner, []))
            self._pending_breakpoints.pop(owner, None)
            self._append_pending_action(
                session_id,
                thread_id,
                "clear_all_breakpoints",
                f"已清除全部源码断点（{removed} 个）",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._persist_session_state(session_id, thread_id, None)
            return await self.get_state(session_id, thread_id)
        async with runtime.state_lock:
            breakpoint_ids = tuple(runtime.inspector_breakpoint_ids.values())
        for inspector_id in breakpoint_ids:
            if runtime.socket is not None:
                await self._command(
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
        self._persist_session_state(session_id, thread_id, runtime)
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
        self._ensure_session_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            pending = self._pending_actions.setdefault(owner, [])
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
                pending[existing_index] = pending[existing_index].model_copy(
                    update={
                        "message": message,
                        "result": result,
                        "actor": "ai",
                        "extension_catalog_binding": extension_catalog_binding,
                    }
                )
            else:
                self._append_pending_action(
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
            self._persist_session_state(session_id, thread_id, None)
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
        self._persist_session_state(session_id, thread_id, runtime)
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
                await self._stop_runtime(runtime)

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
                outcome = await self._stop_runtime(runtime, clear_error=False)
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
                self._persist_session_state(owner[0], owner[1], runtime)

            # runtime 缺失时按 durable claim 恢复合同定点核实旧实例；若
            # claim 仍不可核实，必须阻断删除，而不能把内存缺项当成 stopped。
            decision = await self._reconcile_persisted_claim(owner)
            if decision is not None and decision.outcome == "reconcile_required":
                raise RuntimeError(
                    "删除 Session 前无法核实 Node 调试 claim，"
                    "保持 reconcile_required 并阻断删除: "
                    f"session_id={owner[0]}, thread_id={owner[1]}, "
                    f"reason={decision.reason}"
                )
            claim = self._active_claim(*owner)
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
        await self._reconcile_persisted_claim(owner)
        return owner

    # ---- typed node_debug_process lease：只记录跨 Turn 占用/恢复，不驱动服务行为 ----
    #
    # ThreadResidency 已接线（R5b，OpenSpec 2.8/8.8-A）：已核实的 claim 相位变化经
    # `_sync_residency_blocker` 单向推送为该 (session_id, thread_id) 的 idle blocker
    # （launch_pending/spawned/running/stopping/reconcile_required → 登记；核实终态
    # 且 lease 已结清的 settled → 解除并重新起算 30 分钟 idle）。重启恢复由
    # `residency_blockers`（ResidencyBlockerSource pull 源）兜底：tracker 评估时读
    # 磁盘 durable claim 与在册 runtime 状态，全新 tracker 也不会虚报 cold-eligible。
    #
    # 职责边界（红线）：账本里的 lease 与 residency blocker 都只是占用/阻断上报；
    # 服务判断进程实际状态始终以 durable launch claim + OS 起始身份核实为准。本模块
    # 的读路径不得读取 lease/residency 来推断 running/stopped，也不得因账本缺失或不
    # 一致而虚报终态。

    def _process_lease_identity(
        self, runtime: _NodeDebugRuntime
    ) -> NodeDebugProcessLeaseIdentity | None:
        """按 runtime 的实例身份派生账本 identity；没有实例身份时不登记。"""
        process_instance_id = runtime.process_instance_id
        if process_instance_id is None:
            return None
        return NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=runtime.session_id,
            thread_id=runtime.thread_id,
            process_instance_id=process_instance_id,
        )

    def _ensure_process_lease(self, runtime: _NodeDebugRuntime) -> None:
        """登记 typed ``node_debug_process`` 资源并取得该实例的跨 Turn 占用 lease。

        只在握手成功、claim 进入 running 时调用，且以 lease_id（owner +
        process_instance_id 派生）幂等：同一实例重复调用返回既有占用，不会新增
        第二行，也不会重复 spawn。账本操作失败显式抛出，绝不被吞成“看起来已登记”。
        """
        identity = self._process_lease_identity(runtime)
        if identity is None:
            return
        self._external_resource_leases.register_external(
            resource_id=identity.resource_id,
            kind="node_debug_process",
            lifetime_scope="session",
        )
        self._external_resource_leases.acquire(
            resource_id=identity.resource_id,
            turn_stream_id=identity.holder_id,
            lease_id=identity.lease_id,
            operation_id=identity.operation_id,
        )

    def _settle_process_lease(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None:
        """debug owner 核实进程终态并结清 claim 时，结清同一实例的占用 lease。

        ``reconcile_required`` 与任何“无法核实”的中间态都不调用本方法：占用保持
        active/reconcile_required，作为跨 Turn 的恢复引用供重启后的 owner 读取。
        """
        lease_id = NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=session_id,
            thread_id=thread_id,
            process_instance_id=process_instance_id,
        ).lease_id
        if self._external_resource_leases.get_lease(lease_id) is None:
            # 该实例从未登记过占用（例如 spawn/握手前就终结，或登记本身失败）：
            # 没有可结清的 lease，claim 的核实结果仍是唯一权威，不伪造账本记录。
            return
        self._external_resource_leases.settle(lease_id)

    # ---- launch claim 持久化、代际保护与崩溃恢复 ----

    def _write_launch_claim(self, claim: NodeDebugLaunchClaimDTO) -> None:
        if self._session_store is None:
            # TODO: 无持久化会话树的嵌入式/单测场景没有 thread 节点可登记 claim；
            # 生产接线（app/container.py）始终提供 session_store。
            return
        self._session_store.write_launch_claim(claim)
        # 每次 claim 落盘都是一次已核实的相位变化：同步把 blocker push 给 residency。
        # settled 的写入点都保证先结清 lease 再写终态（见 _mark_claim_phase 与恢复
        # 路径），因此这里的解除天然满足"核实终态 + lease 结清后才解除阻断"。
        self._sync_residency_blocker(claim)

    def _sync_residency_blocker(self, claim: NodeDebugLaunchClaimDTO) -> None:
        """把已核实的 claim 相位变化映射为 ThreadResidency 的 idle blocker 登记/解除。

        push 模型：``ACTIVE_CLAIM_PHASES`` 内的相位 → 按 key（process_instance_id 派生）
        登记/更新 blocker；settled 或未知终态 → 解除同一 key 并从解除时刻重新起算
        idle。reason 用固定脱敏话术，不携带 PID/端口/路径正文。
        """
        if self._residency_tracker is None:
            return
        blocker_key = f"node_debug_claim:{claim.process_instance_id}"
        reason = _NODE_DEBUG_BLOCKER_REASON.get(claim.phase)
        if reason is None:
            # settled（或未来新增的终态）：解除该实例的 idle blocker。
            self._residency_tracker.release_blocker(
                claim.session_id,
                claim.thread_id,
                blocker_key=blocker_key,
            )
            return
        self._residency_tracker.register_blocker(
            claim.session_id,
            claim.thread_id,
            blocker_key=blocker_key,
            kind="node_debug_process",
            reason=reason,
        )

    def residency_blockers(
        self, session_id: str, thread_id: str
    ) -> list[ResidencyBlocker]:
        """ResidencyBlockerSource pull 源：上报该 owner 当前仍活跃的占用。

        重启恢复路径：backend 重启后 tracker 没有任何 push 记录，评估时从这里读
        磁盘 durable claim 与在册 runtime 状态，有活跃占用的 thread 绝不会被虚报为
        cold-eligible。红线：这是"debug owner → residency"的单向占用上报，本服务的
        进程状态判断仍只以 durable claim + OS 身份核实为准，绝不读取 residency。
        """
        owner = self._owner_key(session_id, thread_id)
        blockers: list[ResidencyBlocker] = []
        runtime = self._runtimes.get(owner)
        if (
            runtime is not None
            and runtime.status in _RESIDENCY_ACTIVE_RUNTIME_STATUSES
        ):
            blockers.append(
                ResidencyBlocker(
                    kind="node_debug_process",
                    reason="Node 调试运行时在册且未核实终态",
                )
            )
        claim = self._active_claim(*owner)
        if claim is not None:
            blockers.append(
                ResidencyBlocker(
                    kind="node_debug_process",
                    reason=_NODE_DEBUG_BLOCKER_REASON.get(
                        claim.phase, "Node 调试进程占用该 thread"
                    ),
                )
            )
        return blockers

    def _read_launch_claim(
        self, session_id: str, thread_id: str
    ) -> NodeDebugLaunchClaimDTO | None:
        if self._session_store is None:
            return None
        return self._session_store.read_launch_claim(session_id, thread_id)

    def _active_claim(
        self, session_id: str, thread_id: str
    ) -> NodeDebugLaunchClaimDTO | None:
        """返回仍会阻断新启动的 claim；已结清/不存在时返回 ``None``。"""
        claim = self._read_launch_claim(session_id, thread_id)
        if claim is None or claim.phase not in ACTIVE_CLAIM_PHASES:
            return None
        return claim

    def _claim_for_runtime(
        self, runtime: _NodeDebugRuntime
    ) -> NodeDebugLaunchClaimDTO | None:
        """按 ``process_instance_id`` 取当前实例的 claim，旧 generation 回调不写新实例。"""
        claim = self._read_launch_claim(runtime.session_id, runtime.thread_id)
        if claim is None:
            return None
        if (
            runtime.process_instance_id is None
            or claim.process_instance_id != runtime.process_instance_id
        ):
            return None
        return claim

    def _mark_claim_running(self, runtime: _NodeDebugRuntime) -> None:
        claim = self._claim_for_runtime(runtime)
        if claim is None:
            return
        if claim.phase != "running":
            self._write_launch_claim(
                claim_running(
                    claim,
                    inspector_port=self._authoritative_inspector_port(runtime),
                )
            )
        # 起始身份核对 + Inspector 握手成功之后，才把该 process instance 的占用
        # 登记进唯一账本；重复标记（claim 已是 running）不会新增第二行占用。
        self._ensure_process_lease(runtime)

    @staticmethod
    def _authoritative_inspector_port(runtime: _NodeDebugRuntime) -> int:
        """握手成功后的权威 Inspector 端口。

        Workspace 模板默认使用动态端口（``0``），真实端口只有 Node 上报的握手 URL
        才可信；因此优先取握手地址里的端口，取不到时退回模板配置值。
        """
        inspector_url = runtime.inspector_url
        if inspector_url is not None:
            port = urlparse(inspector_url).port
            if isinstance(port, int) and port > 0:
                return port
        return runtime.inspector_port

    def _mark_claim_phase(
        self,
        runtime: _NodeDebugRuntime,
        phase: Literal["stopping", "reconcile_required", "settled"],
        reason: str | None = None,
    ) -> None:
        claim = self._claim_for_runtime(runtime)
        if claim is None:
            return
        if claim.phase == "settled":
            return
        if phase == "reconcile_required":
            self._notify_release_failed(
                session_id=claim.session_id,
                thread_id=claim.thread_id,
                process_instance_id=claim.process_instance_id,
            )
        if phase == "settled":
            # 先结清账本占用、再写 claim 终态：两步之间崩溃时宁可让 claim 保持
            # active 交由恢复路径再次核实结清，也不能留下“claim 已结清但账本仍
            # 显示占用”的孤儿；反过来则会让幂等的再次结清自然收敛。
            self._settle_process_lease(
                session_id=claim.session_id,
                thread_id=claim.thread_id,
                process_instance_id=claim.process_instance_id,
            )
        self._write_launch_claim(claim_marked(claim, phase=phase, reason=reason))

    def _notify_release_failed(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None:
        """发布 release_failed 轻量通知；失败显式记录，不影响 claim 落盘。"""
        if self._state_events is None:
            return
        resource_id = NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=session_id,
            thread_id=thread_id,
            process_instance_id=process_instance_id,
        ).resource_id
        try:
            self._state_events.publish(resource_id=resource_id, state="release_failed")
        except (RuntimeError, ValueError) as error:
            summary = f"resource_id={resource_id} error={error}"
            logger.exception(
                "resource.state release_failed 事件发布失败: %s",
                summary,
            )

    async def _assert_claim_recoverable(self, owner: NodeDebugOwner) -> None:
        """启动前必须先核实并结清旧 claim；无法核实则拒绝启动新实例。"""
        decision = await self._reconcile_persisted_claim(owner)
        if decision is not None and decision.outcome == "reconcile_required":
            raise RuntimeError(
                "存在无法核实的旧调试实例登记，保持 reconcile_required；"
                f"拒绝启动新实例: session_id={owner[0]}, thread_id={owner[1]}, "
                f"reason={decision.reason}"
            )

    async def _reconcile_persisted_claim(
        self, owner: NodeDebugOwner
    ) -> NodeDebugClaimRecoveryDecision | None:
        """按持久 claim 核实旧实例：能结清的定点结清，无法核实的保持阻断。

        只用于“本进程内没有该 owner 活 runtime”的冷恢复：in-memory runtime 的 claim
        由它自己的 stop/exit 路径结清，绝不能在普通 mutation 里把活进程当旧实例停止。
        """
        if self._runtimes.get(owner) is not None:
            return None
        session_id, thread_id = owner
        claim = self._read_launch_claim(session_id, thread_id)
        if claim is None or claim.phase == "settled":
            return None
        decision = decide_claim_recovery(claim)
        if decision.outcome == "reconcile_required":
            if claim.phase != "reconcile_required":
                # 只在“进入”该状态时写登记并留一条审计动作；状态本身可反复查询，
                # 但读接口是轮询入口，绝不能每次轮询都追加动作并重写 manifest。
                claim = claim_marked(
                    claim, phase="reconcile_required", reason=decision.reason
                )
                self._write_launch_claim(claim)
                self._record_claim_action(
                    owner,
                    "reconcile_required",
                    f"调试实例无法核实，需人工核实后才能继续: {decision.reason}",
                    result="error",
                )
                self._notify_release_failed(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
            return decision
        if decision.outcome == "terminate_then_settle":
            terminated = await self._terminate_verified_instance(
                pid=claim.pid,
                recorded_source=claim.process_identity_source,
                recorded_start_marker=claim.process_start_marker,
            )
            if not terminated:
                failure = (
                    "已核实为登记的同一实例但停止失败，保持 reconcile_required: "
                    f"pid={claim.pid}"
                )
                self._write_launch_claim(
                    claim_marked(
                        claim,
                        phase="reconcile_required",
                        reason=failure,
                    )
                )
                self._record_claim_action(
                    owner, "reconcile_required", failure, result="error"
                )
                self._notify_release_failed(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
                return NodeDebugClaimRecoveryDecision(
                    outcome="reconcile_required",
                    reason=failure,
                    identity=decision.identity,
                )
        # 已核实旧实例不存在（或已按 owner 策略定点停止）：结清账本占用后才写
        # claim 终态；没有任何登记（例如登记前崩溃）时账本保持原样、不重复 acquire。
        self._settle_process_lease(
            session_id=claim.session_id,
            thread_id=claim.thread_id,
            process_instance_id=claim.process_instance_id,
        )
        self._write_launch_claim(
            claim_marked(claim, phase="settled", reason=decision.reason)
        )
        self._record_claim_action(
            owner,
            "reconcile_settled",
            f"已结清遗留调试实例登记: {decision.reason}",
        )
        return NodeDebugClaimRecoveryDecision(
            outcome="settle",
            reason=decision.reason,
            identity=decision.identity,
        )

    async def _wait_for_recorded_instance(
        self,
        *,
        pid: int,
        recorded_source: str | None,
        recorded_start_marker: str,
        timeout_seconds: float,
    ) -> Literal["terminated", "reused", "incomparable", "same"]:
        """轮询该 PID，直到事实可判定或超时。

        每轮都重新比对身份，绝不把"probe 返回了某个东西"当成"仍是登记的那个实例"：
        等待窗口内 PID 可能被回收复用，届时只有 ``reused``/``terminated`` 才是终结证据，
        升级 SIGKILL 的前提是本轮确认过 ``same``。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            identity = probe_process_identity(pid)
            if identity is None:
                return "terminated"
            comparison = identity.compare(
                recorded_source=recorded_source,
                recorded_start_marker=recorded_start_marker,
            )
            if comparison != "match":
                return "reused" if comparison == "mismatch" else "incomparable"
            if loop.time() >= deadline:
                return "same"
            await asyncio.sleep(0.01)

    async def _terminate_verified_instance(
        self,
        *,
        pid: int | None,
        recorded_source: str | None,
        recorded_start_marker: str | None,
    ) -> bool:
        """只终止“再次核实为同一实例”的进程；PID 复用一律不碰，事实不足也不碰。"""
        if pid is None or recorded_start_marker is None:
            return False
        state = await self._wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=0.0,
        )
        if state == "terminated":
            return True
        if state == "reused":
            # PID 已被同来源的新实例复用：原实例已不存在，绝不停止新进程。
            return True
        if state == "incomparable":
            # 来源不可比对（含跨来源）＝事实不足，既不认领也不停止，保持阻断。
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # 就在刚才已终结：可核实的终态。
            return True
        state = await self._wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=_RECONCILE_TERMINATE_TIMEOUT_SECONDS,
        )
        if state != "same":
            # SIGTERM 窗口内可能发生了 PID 复用：只接受 reused/terminated 作为结清证据，
            # incomparable 一律视为未核实，绝不升级 SIGKILL。
            return state in {"terminated", "reused"}
        # 此处已重新核实"仍是登记的那一个实例"，才允许升级到强制终止。
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        state = await self._wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=_RECONCILE_TERMINATE_TIMEOUT_SECONDS,
        )
        return state in {"terminated", "reused"}

    def _record_claim_action(
        self,
        owner: NodeDebugOwner,
        action: str,
        message: str,
        *,
        result: Literal["success", "error"] = "success",
    ) -> None:
        session_id, thread_id = owner
        self._append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor="system",
            tool_name=None,
            tool_call_id=None,
            result=result,
        )
        self._write_session_manifest(session_id, thread_id)

    def _ensure_session_loaded(self, session_id: str, thread_id: str) -> None:
        owner = self._owner_key(session_id, thread_id)
        manifest = self._configuration_registry.ensure_loaded(session_id, thread_id)
        if manifest is None:
            return
        self._pending_actions[owner] = [
            action.model_copy(deep=True) for action in manifest.actions[-_MAX_ACTIONS:]
        ]
        if manifest.active_configuration_id is not None:
            self._load_active_configuration(session_id, thread_id)

    def _persist_session_state(
        self,
        session_id: str,
        thread_id: str,
        runtime: _NodeDebugRuntime | None,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        configuration_id = self._configuration_registry.active_id(session_id, thread_id)
        selection = self._launch_selections.get(
            owner,
            _NodeDebugLaunchSelection(),
        )
        if runtime is not None:
            configuration_id = runtime.configuration_id
            self._configuration_registry.set_active(
                session_id, configuration_id, thread_id
            )
            selection = _NodeDebugLaunchSelection(
                script_path=runtime.relative_script_path,
                working_directory=(
                    runtime.working_directory.relative_to(
                        runtime.workspace_root
                    ).as_posix()
                    if runtime.working_directory != runtime.workspace_root
                    else ""
                ),
                launch_profile_name=runtime.launch_profile_name,
                args=list(runtime.args),
            )
            self._launch_selections[owner] = selection
            breakpoints = [
                persistable_breakpoint(breakpoint)
                for breakpoint in runtime.breakpoints.values()
            ]
            self._pending_actions[owner] = [
                action.model_copy(deep=True)
                for action in runtime.actions[-_MAX_ACTIONS:]
            ]
        else:
            breakpoints = [
                persistable_breakpoint(breakpoint)
                for breakpoint in self._pending_breakpoints.get(owner, [])
            ]
            self._pending_actions.setdefault(owner, [])
        if configuration_id is not None:
            current = self._configuration(session_id, thread_id, configuration_id)
            normalized_breakpoints = [
                portable_breakpoint(breakpoint) for breakpoint in breakpoints
            ]
            configuration_changed = (
                current.script_path != selection.script_path
                or current.working_directory != (selection.working_directory or "")
                or current.launch_profile_name != selection.launch_profile_name
                or current.args != list(selection.args)
                or current.breakpoints != normalized_breakpoints
            )
            if configuration_changed:
                configuration = current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "script_path": selection.script_path,
                        "working_directory": selection.working_directory or "",
                        "launch_profile_name": selection.launch_profile_name,
                        "args": list(selection.args),
                        "breakpoints": normalized_breakpoints,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._configuration_registry.put(
                    session_id, configuration, thread_id
                )
        self._write_session_manifest(session_id, thread_id)

    def _validate_configuration(
        self,
        configuration: NodeDebugConfigurationDTO,
    ) -> NodeDebugConfigurationDTO:
        if configuration.script_path is not None:
            _, relative_path = self._resolve_script_path(configuration.script_path)
            configuration = configuration.model_copy(
                update={"script_path": relative_path}
            )
        resolved_directory = self._resolve_working_directory(
            configuration.working_directory
        )
        relative_directory = (
            resolved_directory.relative_to(self._workspace_root).as_posix()
            if resolved_directory != self._workspace_root
            else ""
        )
        normalized_breakpoints: list[NodeDebugBreakpointDTO] = []
        for breakpoint in configuration.breakpoints:
            breakpoint_path, relative_path = self._resolve_script_path(breakpoint.path)
            normalized_breakpoints.append(
                persistable_breakpoint(
                    reconcile_breakpoint(
                        runtime_breakpoint(
                            breakpoint.model_copy(update={"path": relative_path})
                        ),
                        breakpoint_path,
                    )
                )
            )
        return configuration.model_copy(
            update={
                "name": configuration.name.strip(),
                "working_directory": relative_directory,
                "args": self._normalize_args(configuration.args),
                "breakpoints": [
                    portable_breakpoint(breakpoint)
                    for breakpoint in normalized_breakpoints
                ],
            }
        )

    def _configuration_from_request(
        self,
        *,
        configuration_id: str,
        name: str,
        script_path: str | None,
        working_directory: str,
        launch_profile_name: str | None,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        revision: int = 1,
        created_at: datetime | None = None,
    ) -> NodeDebugConfigurationDTO:
        now = datetime.now(UTC)
        configuration = NodeDebugConfigurationDTO(
            configuration_id=configuration_id,
            name=name.strip(),
            revision=revision,
            script_path=script_path,
            working_directory=working_directory,
            launch_profile_name=launch_profile_name,
            args=list(args),
            breakpoints=[
                portable_breakpoint(
                    self._create_breakpoint(
                        path=breakpoint.path,
                        line=breakpoint.line,
                        column=breakpoint.column,
                        condition=breakpoint.condition,
                        hit_condition=breakpoint.hit_condition,
                        log_message=breakpoint.log_message,
                    )
                )
                for breakpoint in breakpoints
            ],
            created_at=created_at or now,
            updated_at=now,
        )
        return self._validate_configuration(configuration)

    def _select_configuration_for_start(
        self,
        *,
        session_id: str,
        thread_id: str,
        configuration_id: str | None,
        path: str,
        working_directory: str | None,
        launch_profile_name: str | None,
        args: list[str],
    ) -> str:
        selected_id = configuration_id or self._configuration_registry.active_id(
            session_id, thread_id
        )
        if selected_id is None:
            _, relative_path = self._resolve_script_path(path)
            configuration = self._configuration_from_request(
                configuration_id=create_prefixed_id("dbgcfg"),
                name=f"调试 {Path(relative_path).name}",
                script_path=relative_path,
                working_directory=working_directory or "",
                launch_profile_name=launch_profile_name,
                args=args,
                breakpoints=[],
            )
            self._configuration_registry.put(session_id, configuration, thread_id)
            selected_id = configuration.configuration_id
        self._configuration(session_id, thread_id, selected_id)
        if self._configuration_registry.active_id(session_id, thread_id) != selected_id:
            self._activate_configuration_in_memory(session_id, thread_id, selected_id)
        return selected_id

    def _ensure_configuration_for_breakpoint(
        self,
        session_id: str,
        thread_id: str,
        params: dict[str, object],
    ) -> None:
        if self._configuration_registry.active_id(session_id, thread_id) is not None:
            return
        raw_path = params.get("path")
        if not isinstance(raw_path, str):
            raise TypeError("首次设置源码断点必须提供 path")
        _, relative_path = self._resolve_script_path(raw_path)
        configuration = self._configuration_from_request(
            configuration_id=create_prefixed_id("dbgcfg"),
            name=f"调试 {Path(relative_path).name}",
            script_path=relative_path,
            working_directory="",
            launch_profile_name="node-default",
            args=[],
            breakpoints=[],
        )
        self._configuration_registry.put(session_id, configuration, thread_id)
        self._activate_configuration_in_memory(
            session_id,
            thread_id,
            configuration.configuration_id,
        )

    def _activate_configuration_in_memory(
        self,
        session_id: str,
        thread_id: str,
        configuration_id: str,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        self._configuration(session_id, thread_id, configuration_id)
        runtime = self._runtimes.get(owner)
        if runtime is not None and runtime.status not in {
            "starting",
            "running",
            "paused",
        }:
            self._runtimes.pop(owner, None)
        self._configuration_registry.set_active(session_id, configuration_id, thread_id)
        self._load_active_configuration(session_id, thread_id)

    def _load_active_configuration(self, session_id: str, thread_id: str) -> None:
        owner = self._owner_key(session_id, thread_id)
        configuration_id = self._configuration_registry.active_id(session_id, thread_id)
        if configuration_id is None:
            raise RuntimeError(
                f"会话没有活动调试方案: session_id={session_id}, thread_id={thread_id}"
            )
        configuration = self._configuration(session_id, thread_id, configuration_id)
        self._launch_selections[owner] = _NodeDebugLaunchSelection(
            script_path=configuration.script_path,
            working_directory=configuration.working_directory,
            launch_profile_name=configuration.launch_profile_name,
            args=list(configuration.args),
        )
        self._pending_breakpoints[owner] = [
            persistable_breakpoint(runtime_breakpoint(breakpoint))
            for breakpoint in configuration.breakpoints
        ]

    def _configuration(
        self,
        session_id: str,
        thread_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO:
        return self._configuration_registry.get(session_id, configuration_id, thread_id)

    def _write_session_manifest(self, session_id: str, thread_id: str) -> None:
        self._configuration_registry.write_manifest(
            NodeDebugSessionManifestDTO(
                session_id=session_id,
                thread_id=thread_id,
                active_configuration_id=self._configuration_registry.active_id(
                    session_id, thread_id
                ),
                actions=[
                    action.model_copy(deep=True)
                    for action in self._pending_actions.get(
                        self._owner_key(session_id, thread_id), []
                    )[
                        -_MAX_ACTIONS:
                    ]
                ],
                updated_at=datetime.now(UTC),
            )
        )

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
            self._pending_actions[owner] = [
                item.model_copy(deep=True) for item in runtime.actions[-_MAX_ACTIONS:]
            ]
            return
        self._append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

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
        claim = self._active_claim(*owner)
        if claim is None:
            return
        raise RuntimeError(
            f"{operation}被未结清的调试实例登记阻断: "
            f"phase={claim.phase}, session_id={owner[0]}, thread_id={owner[1]}, "
            f"reason={claim.reconcile_reason or '等待核实旧实例终态'}"
        )

    def _create_breakpoint(
        self,
        *,
        path: str,
        line: int,
        column: int,
        condition: str | None,
        hit_condition: int | None = None,
        log_message: str | None = None,
    ) -> NodeDebugBreakpointDTO:
        script_path, relative_path = self._resolve_script_path(path)
        breakpoint = NodeDebugBreakpointDTO(
            breakpoint_id=create_prefixed_id("node-bp"),
            path=relative_path,
            line=line,
            column=column,
            condition=condition.strip() or None if condition is not None else None,
            hit_condition=hit_condition,
            log_message=log_message,
            original_line=line,
            created_at=datetime.now(UTC),
        )
        inspector_breakpoint_condition(
            breakpoint_id=breakpoint.breakpoint_id,
            condition=breakpoint.condition,
            hit_condition=breakpoint.hit_condition,
            log_message=breakpoint.log_message,
        )
        return anchor_breakpoint(breakpoint, script_path)

    def _source_digests_for_runtime(
        self,
        runtime: _NodeDebugRuntime,
    ) -> dict[str, str | None]:
        paths = {
            runtime.relative_script_path,
            *(breakpoint.path for breakpoint in runtime.breakpoints.values()),
        }
        return {
            path: source_digest(safe_join(runtime.workspace_root, path))
            for path in paths
        }

    async def _reconcile_session_sources(
        self,
        session_id: str,
        thread_id: str,
        runtime: _NodeDebugRuntime | None,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        breakpoints = (
            list(runtime.breakpoints.values())
            if runtime is not None
            else list(self._pending_breakpoints.get(owner, []))
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
            if (
                runtime is not None
                and next_breakpoint.relocation_status != "current"
            ):
                inspector_id = runtime.inspector_breakpoint_ids.get(
                    breakpoint.breakpoint_id
                )
                if inspector_id is not None:
                    invalidated_inspector_ids.append(
                        (breakpoint.breakpoint_id, inspector_id)
                    )

        if runtime is not None:
            active = runtime.status in {"starting", "running", "paused"}
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
            if invalidated_inspector_ids and runtime.socket is not None:
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
            self._pending_breakpoints[owner] = reconciled
            pending_actions = self._pending_actions.setdefault(owner, [])
            for message in relocation_messages:
                pending_actions.append(
                    NodeDebugActionRecordDTO(
                        action_id=create_prefixed_id("node-debug-action"),
                        session_id=session_id,
                        thread_id=thread_id,
                        action="breakpoint_reconciled",
                        message=message,
                        actor="system",
                        created_at=datetime.now(UTC),
                    )
                )
            del pending_actions[:-_MAX_ACTIONS]

        if should_persist:
            self._persist_session_state(session_id, thread_id, runtime)

    def _get_debug_runtime_config(self) -> dict[str, object]:
        return self._config_service.get_debug_runtime_config()

    @staticmethod
    def _resolve_launch_profile(
        debug_config: dict[str, object],
        launch_profile_name: str | None,
    ) -> tuple[str, dict[str, object]]:
        raw_profiles = debug_config.get("launch_profiles")
        if not isinstance(raw_profiles, dict):
            raise TypeError("runtime.debug.launch_profiles 配置无效")
        profile_name = launch_profile_name or "node-default"
        raw_profile = raw_profiles.get(profile_name)
        if raw_profile is None and launch_profile_name is None:
            raw_profile = {
                "adapter": debug_config.get("default_adapter", "node_inspector"),
                "runtime": "node",
                "program": "",
                "working_directory": "",
                "args": [],
            }
        if not isinstance(raw_profile, dict):
            raise TypeError(f"调试启动配置不存在: {profile_name}")
        return profile_name, raw_profile

    def _resolve_working_directory(self, raw_path: str) -> Path:
        normalized = raw_path.strip()
        if not normalized:
            return self._workspace_root
        candidate = Path(normalized)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self._workspace_root)
            except ValueError as error:
                raise ValueError(
                    f"调试工作目录必须位于当前 workspace 内: {normalized}"
                ) from error
            if not resolved.is_dir():
                raise FileNotFoundError(f"调试工作目录不存在: {normalized}")
            return resolved
        resolved = safe_join(self._workspace_root, normalized)
        if not resolved.is_dir():
            raise FileNotFoundError(f"调试工作目录不存在: {normalized}")
        return resolved

    async def _set_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: _NodeDebugRuntime | None,
        params: dict[str, object],
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        raw_path = params.get(
            "path",
            runtime.relative_script_path if runtime is not None else None,
        )
        if not isinstance(raw_path, str):
            raise TypeError("源码断点 path 必须是字符串")
        line = self._positive_int(params.get("line"), "line")
        column = self._positive_int(params.get("column", 1), "column")
        condition = params.get("condition")
        if condition is not None and not isinstance(condition, str):
            raise TypeError("源码断点 condition 必须是字符串")
        hit_condition = params.get("hit_condition")
        if hit_condition is not None:
            hit_condition = self._positive_int(hit_condition, "hit_condition")
        log_message = params.get("log_message")
        if log_message is not None and not isinstance(log_message, str):
            raise TypeError("源码断点 log_message 必须是字符串")
        breakpoint = self._create_breakpoint(
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
            if (
                self._matching_breakpoint(
                    pending,
                    breakpoint,
                )
                is not None
            ):
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
            if (
                self._matching_breakpoint(
                    runtime.breakpoints.values(),
                    breakpoint,
                )
                is not None
            ):
                raise ValueError(f"源码断点已存在: {breakpoint.path}:{line}:{column}")
            runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint
        if runtime.socket is not None and runtime.status in {"running", "paused"}:
            await self._install_breakpoint(runtime, breakpoint, script_path=script_path)
        async with runtime.state_lock:
            self._append_action(
                runtime,
                "set_breakpoint",
                f"已设置源码断点 {breakpoint.path}:{line}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
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

    async def _update_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: _NodeDebugRuntime | None,
        params: dict[str, object],
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        breakpoint_id = params.get("breakpoint_id")
        if not isinstance(breakpoint_id, str) or not breakpoint_id.strip():
            raise ValueError("编辑源码断点必须提供 breakpoint_id")
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

        raw_path = params.get("path", current.path)
        if not isinstance(raw_path, str):
            raise TypeError("源码断点 path 必须是字符串")
        line = self._positive_int(params.get("line", current.line), "line")
        column = self._positive_int(params.get("column", current.column), "column")
        condition = params.get("condition", current.condition)
        if condition is not None and not isinstance(condition, str):
            raise TypeError("源码断点 condition 必须是字符串")
        hit_condition = params.get("hit_condition", current.hit_condition)
        if hit_condition is not None:
            hit_condition = self._positive_int(hit_condition, "hit_condition")
        log_message = params.get("log_message", current.log_message)
        if log_message is not None and not isinstance(log_message, str):
            raise TypeError("源码断点 log_message 必须是字符串")
        updated = self._create_breakpoint(
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
        if runtime.socket is not None and runtime.status in {"running", "paused"}:
            await self._install_breakpoint(
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

    async def _clear_breakpoint(
        self,
        owner: NodeDebugOwner,
        runtime: _NodeDebugRuntime | None,
        params: dict[str, object],
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        breakpoint_id = params.get("breakpoint_id")
        if not isinstance(breakpoint_id, str) or not breakpoint_id.strip():
            raise ValueError("清除源码断点必须提供 breakpoint_id")
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
        if inspector_id and runtime.socket is not None:
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

    async def _install_breakpoint(
        self,
        runtime: _NodeDebugRuntime,
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

    async def _evaluate(
        self,
        runtime: _NodeDebugRuntime,
        params: dict[str, object],
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        expression = params.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("表达式不能为空")
        async with runtime.state_lock:
            if runtime.status != "paused" or not runtime.call_stack:
                raise RuntimeError("只有暂停在源码断点时才能求值")
            call_frame_id = runtime.call_stack[0].call_frame_id
        result = await self._command(
            runtime,
            "Debugger.evaluateOnCallFrame",
            {
                "callFrameId": call_frame_id,
                "expression": expression,
                "returnByValue": True,
                "generatePreview": False,
            },
        )
        remote_result = result.get("result")
        exception_details = result.get("exceptionDetails")
        evaluation = NodeDebugEvaluationDTO(
            expression=expression,
            value=self._remote_value(remote_result),
            type=self._remote_type(remote_result),
            description=self._remote_description(remote_result),
            error=(
                self._exception_message(exception_details)
                if isinstance(exception_details, dict)
                else None
            ),
            evaluated_at=datetime.now(UTC),
        )
        async with runtime.state_lock:
            runtime.last_evaluation = evaluation
            runtime.evaluations.append(evaluation)
            del runtime.evaluations[:-_MAX_ACTIONS]
            runtime.error_message = None
            self._append_action(
                runtime,
                "evaluate",
                f"已求值: {expression}",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def _debugger_command(
        self,
        runtime: _NodeDebugRuntime,
        action: NodeDebugAction,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        if runtime.socket is None or runtime.status not in {"running", "paused"}:
            raise RuntimeError("Node 调试进程当前不可控制")
        method_by_action = {
            "continue": "Debugger.resume",
            "pause": "Debugger.pause",
            "step_over": "Debugger.stepOver",
            "step_into": "Debugger.stepInto",
            "step_out": "Debugger.stepOut",
        }
        method = method_by_action.get(action)
        if method is None:
            raise ValueError(f"不支持的 Node 调试动作: {action}")
        if action in {"continue", "step_over", "step_into", "step_out"}:
            async with runtime.state_lock:
                runtime.status = "running"
                self._clear_paused_snapshot(runtime)
        await self._command(runtime, method)
        if action in {"continue", "pause"}:
            await self._wait_for_execution_state(runtime)
            await self._wait_for_frame_variables(runtime)
        if action in {"step_over", "step_into", "step_out"}:
            await self._wait_for_execution_state(runtime)
            await self._wait_for_frame_variables(runtime)
        async with runtime.state_lock:
            message = {
                "continue": "已继续执行 JavaScript",
                "pause": "已请求暂停 JavaScript",
                "step_over": "已执行一步单步跳过",
                "step_into": "已执行一步单步进入",
                "step_out": "已执行一步单步跳出",
            }[action]
            self._append_action(
                runtime,
                action,
                message,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    async def _command(
        self,
        runtime: _NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        socket = runtime.socket
        if socket is None:
            raise RuntimeError("Node Inspector WebSocket 尚未连接")
        async with runtime.command_lock:
            command_id = runtime.next_command_id
            runtime.next_command_id += 1
            future: asyncio.Future[dict[str, object]] = (
                asyncio.get_running_loop().create_future()
            )
            runtime.pending_commands[command_id] = future
            await socket.send(
                json.dumps(
                    {"id": command_id, "method": method, "params": params or {}},
                    ensure_ascii=False,
                )
            )
            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=runtime.command_timeout_seconds,
                )
            finally:
                runtime.pending_commands.pop(command_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "Node Inspector 命令失败"
            raise RuntimeError(str(message))  # noqa: TRY004 - 这是远端协议错误
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise TypeError(f"Node Inspector 响应 result 不是对象: {result!r}")
        return cast(dict[str, object], result)

    async def _receive_messages(self, runtime: _NodeDebugRuntime) -> None:
        socket = runtime.socket
        if socket is None:
            return
        try:
            async for raw_message in socket:
                payload = json.loads(raw_message)
                if not isinstance(payload, dict):
                    raise TypeError(f"Node Inspector 消息不是对象: {payload!r}")
                command_id = payload.get("id")
                if isinstance(command_id, int):
                    future = runtime.pending_commands.get(command_id)
                    if future is not None and not future.done():
                        future.set_result(cast(dict[str, object], payload))
                    continue
                method = payload.get("method")
                params = payload.get("params")
                if isinstance(method, str) and isinstance(params, dict):
                    await self._handle_event(runtime, method, params)
        except ConnectionClosed:
            if not runtime.closing:
                async with runtime.state_lock:
                    if runtime.process is not None and runtime.process.returncode == 0:
                        runtime.status = "exited"
                        runtime.error_message = runtime.logpoint_error_message
                    else:
                        runtime.status = "failed"
                        runtime.error_message = (
                            runtime.logpoint_error_message
                            or "Node Inspector WebSocket 已断开"
                        )
                    self._clear_stop_snapshot(runtime)
        except Exception as error:  # noqa: BLE001 - 接收循环必须将适配器故障写入状态
            if not runtime.closing:
                async with runtime.state_lock:
                    runtime.status = "failed"
                    runtime.error_message = (
                        runtime.logpoint_error_message
                        or f"读取 Node Inspector 事件失败: {error}"
                    )
                    self._clear_stop_snapshot(runtime)
        finally:
            error = RuntimeError("Node Inspector WebSocket 已关闭")
            for future in tuple(runtime.pending_commands.values()):
                if not future.done():
                    future.set_exception(error)

    async def _handle_event(
        self,
        runtime: _NodeDebugRuntime,
        method: str,
        params: dict[str, object],
    ) -> None:
        if method == "Debugger.scriptParsed":
            script_id = params.get("scriptId")
            url = params.get("url")
            if isinstance(script_id, str) and isinstance(url, str) and url:
                runtime.script_urls[script_id] = url
        elif method == "Debugger.paused":
            call_frames = params.get("callFrames")
            frames = self._parse_call_frames(runtime, call_frames)
            hit_breakpoints = params.get("hitBreakpoints")
            async with runtime.state_lock:
                runtime.status = "paused"
                runtime.paused_reason = self._string_or_none(params.get("reason"))
                runtime.paused_breakpoint_ids = (
                    {
                        breakpoint_id
                        for breakpoint_id in hit_breakpoints
                        if isinstance(breakpoint_id, str)
                    }
                    if isinstance(hit_breakpoints, list)
                    else set()
                )
                runtime.error_message = runtime.logpoint_error_message
                runtime.scope_object_ids = self._scope_object_ids(call_frames)
                runtime.call_stack = frames
                if frames:
                    frame = frames[0]
                    runtime.last_stopped_frame = frame.model_copy(deep=True)
                    for breakpoint_id, breakpoint in runtime.breakpoints.items():
                        if (
                            breakpoint.path != frame.path
                            or breakpoint.line != frame.line
                        ):
                            continue
                        runtime.breakpoints[breakpoint_id] = breakpoint.model_copy(
                            update={
                                "verified": True,
                                "actual_line": frame.line,
                                "inspector_id": runtime.inspector_breakpoint_ids.get(
                                    breakpoint_id
                                ),
                            }
                        )
            if frames:
                runtime.variable_hydration_task = asyncio.create_task(
                    self._hydrate_frame_variables_safe(runtime, frames[0]),
                )
        elif method == "Debugger.resumed":
            async with runtime.state_lock:
                runtime.status = (
                    "exited"
                    if runtime.process is not None
                    and runtime.process.returncode is not None
                    else "running"
                )
                runtime.error_message = runtime.logpoint_error_message
                self._clear_paused_snapshot(runtime)
        elif method == "NodeRuntime.waitingForDisconnect":
            socket = runtime.socket
            if socket is not None:
                await socket.close()
                runtime.socket = None

    async def _hydrate_frame_variables_safe(
        self,
        runtime: _NodeDebugRuntime,
        frame: NodeDebugStackFrameDTO,
    ) -> None:
        try:
            await self._hydrate_frame_variables(runtime, frame)
        except Exception as error:  # noqa: BLE001 - 变量读取故障必须暴露在调试状态
            if "Cannot find context with specified id" in str(error):
                return
            async with runtime.state_lock:
                if (
                    runtime.status == "paused"
                    and runtime.call_stack
                    and runtime.call_stack[0].call_frame_id == frame.call_frame_id
                ):
                    runtime.error_message = f"读取局部变量失败: {error}"

    async def _hydrate_frame_variables(
        self,
        runtime: _NodeDebugRuntime,
        frame: NodeDebugStackFrameDTO,
    ) -> None:
        object_ids = runtime.scope_object_ids.get(frame.call_frame_id, {})
        variables: list[NodeDebugVariableDTO] = []
        expired_object_count = 0
        for scope, scope_object_ids in object_ids.items():
            for object_id in scope_object_ids[:3]:
                try:
                    result = await self._command(
                        runtime,
                        "Runtime.getProperties",
                        {
                            "objectId": object_id,
                            "ownProperties": True,
                            "accessorPropertiesOnly": False,
                        },
                    )
                except RuntimeError as error:
                    if "Could not find object with given id" not in str(error):
                        raise
                    expired_object_count += 1
                    continue
                properties = result.get("result")
                if not isinstance(properties, list):
                    continue
                for property_value in properties:
                    if not isinstance(property_value, dict):
                        continue
                    name = property_value.get("name")
                    if not isinstance(name, str):
                        continue
                    remote_value = property_value.get("value")
                    variables.append(
                        NodeDebugVariableDTO(
                            name=name,
                            value=self._remote_value(remote_value) or "undefined",
                            type=self._remote_type(remote_value),
                            object_id=self._remote_object_id(remote_value),
                            scope=scope,
                        )
                    )
        async with runtime.state_lock:
            if runtime.status != "paused" or not runtime.call_stack:
                return
            if runtime.call_stack[0].call_frame_id != frame.call_frame_id:
                return
            runtime.call_stack[0] = frame.model_copy(update={"variables": variables})
            if variables:
                runtime.error_message = runtime.logpoint_error_message
                if expired_object_count:
                    self._append_action(
                        runtime,
                        "variable_scope_skipped",
                        f"已跳过 {expired_object_count} 个失效的 Inspector 变量对象；其余变量已返回",
                        actor="system",
                        result="error",
                    )
            elif expired_object_count:
                runtime.error_message = (
                    "读取局部变量失败：暂停期间 Inspector 变量对象已经失效"
                )

    async def _read_stream(
        self,
        runtime: _NodeDebugRuntime,
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
                runtime.inspector_url = match.group(1)
                runtime.inspector_ready.set()
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

    async def _monitor_process(self, runtime: _NodeDebugRuntime) -> None:
        process = runtime.process
        if process is None:
            return
        return_code = await process.wait()
        # 进程句柄已报告终态：这是可核实的终结，结清本实例的 claim。
        self._mark_claim_phase(
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

    async def _wait_for_execution_state(self, runtime: _NodeDebugRuntime) -> None:
        for _ in range(200):
            async with runtime.state_lock:
                if runtime.status in {"paused", "exited", "failed"}:
                    return
            await asyncio.sleep(0.01)

    async def _wait_for_frame_variables(self, runtime: _NodeDebugRuntime) -> None:
        for _ in range(100):
            task = runtime.variable_hydration_task
            if task is not None:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=runtime.command_timeout_seconds,
                )
                return
            async with runtime.state_lock:
                if runtime.status != "paused":
                    return
            await asyncio.sleep(0.01)

    @staticmethod
    def _paused_at_breakpoint(runtime: _NodeDebugRuntime) -> bool:
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

    async def _stop_runtime(
        self,
        runtime: _NodeDebugRuntime,
        *,
        clear_error: bool = True,
    ) -> Literal["stopped", "reconcile_required"]:
        """停止并核实进程终结；未核实终结时保持 ``reconcile_required`` 阻断。

        返回 ``stopped`` 时进程句柄已确认终结且 claim 已结清，但本方法按既有语义把
        ``runtime.status`` 留在 ``stopping``，由调用方在核实后置终态；返回
        ``reconcile_required`` 时不得解除阻断、不得启动新实例。
        """
        async with runtime.state_lock:
            if runtime.status in {"starting", "running", "paused"}:
                # 进程尚未真实退出前必须保持 thread 的活跃阻断；调用方
                # 可能在此期间查询状态或尝试删除 thread，不能提前显示 exited。
                runtime.status = "stopping"
            runtime.closing = True
        self._mark_claim_phase(runtime, "stopping", "收到停止请求，等待进程终结")
        socket = runtime.socket
        if socket is not None:
            await socket.close()
            runtime.socket = None
        failure_reason = await self._terminate_and_verify(runtime)
        tasks = (
            runtime.receiver_task,
            runtime.stderr_task,
            runtime.stdout_task,
            runtime.process_task,
        )
        current_task = asyncio.current_task()
        for task in tasks:
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        pending = [
            task for task in tasks if task is not None and task is not current_task
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with runtime.state_lock:
            self._clear_stop_snapshot(runtime)
        if failure_reason is None:
            self._mark_claim_phase(runtime, "settled", "已核实进程终结并结清")
            async with runtime.state_lock:
                if clear_error:
                    runtime.error_message = None
            return "stopped"
        self._mark_claim_phase(runtime, "reconcile_required", failure_reason)
        async with runtime.state_lock:
            # 无法核实终态：绝不能虚报 exited/stopped，保持 reconcile_required 阻断。
            runtime.status = "reconcile_required"
            runtime.error_message = (
                f"停止调试进程失败且无法核实终态: {failure_reason}"
            )
            self._append_action(
                runtime,
                "stop_reconcile_required",
                f"停止调试进程失败且无法核实终态: {failure_reason}",
                actor="system",
                result="error",
            )
        return "reconcile_required"

    async def _terminate_and_verify(self, runtime: _NodeDebugRuntime) -> str | None:
        """终止 runtime 进程并核实终结；返回 ``None`` 表示已核实不存在。"""
        process = runtime.process
        if process is None:
            # 尚未 spawn：可以证明不存在该实例。
            return None
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except OSError as error:
                self._append_action(
                    runtime,
                    "stop_signal_failed",
                    f"发送终止信号失败: {error}",
                    actor="system",
                    result="error",
                )
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    self._append_action(
                        runtime,
                        "stop_kill_failed",
                        f"强制终止调试进程失败: {error}",
                        actor="system",
                        result="error",
                    )
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_KILL_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    pass
            except OSError as error:
                self._append_action(
                    runtime,
                    "stop_wait_failed",
                    f"等待调试进程退出失败: {error}",
                    actor="system",
                    result="error",
                )
        if process.returncode is not None:
            return None
        return self._verify_process_gone(runtime)

    def _verify_process_gone(self, runtime: _NodeDebugRuntime) -> str | None:
        """句柄无法确认终结时按记录的 OS 起始身份核实；``None`` 表示已核实不存在。"""
        process = runtime.process
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "进程句柄未报告终态且缺少可用 PID"
        identity = probe_process_identity(pid)
        if identity is None:
            return None
        if runtime.process_start_marker is None:
            return (
                "缺少可核实的 OS 进程起始身份，不能判定终结: "
                f"pid={pid}, source={identity.source}"
            )
        comparison = identity.compare(
            recorded_source=runtime.process_identity_source,
            recorded_start_marker=runtime.process_start_marker,
        )
        if comparison == "match":
            return f"进程仍存活且起始身份匹配: pid={pid}"
        if comparison == "incomparable":
            # 跨来源或标记缺失＝事实不足：不能把"比不出来"当成进程已终结，否则会虚报 exited。
            return (
                "PID 当前实例的起始身份与登记不可比对，无法核实是否同一实例: "
                f"pid={pid}, recorded_source={runtime.process_identity_source}, "
                f"actual_source={identity.source}"
            )
        # 同来源但起始身份不同：PID 已被复用，原实例已不存在，也不停止新进程。
        return None

    @staticmethod
    def _clear_paused_snapshot(runtime: _NodeDebugRuntime) -> None:
        runtime.paused_reason = None
        runtime.paused_breakpoint_ids.clear()
        runtime.call_stack.clear()
        runtime.scope_object_ids.clear()
        runtime.last_evaluation = None

    @classmethod
    def _clear_stop_snapshot(cls, runtime: _NodeDebugRuntime) -> None:
        cls._clear_paused_snapshot(runtime)
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

    def _resolve_script_path(self, raw_path: str) -> tuple[Path, str]:
        normalized = raw_path.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("Node 调试脚本路径不能为空")
        script_path = safe_join(self._workspace_root, normalized)
        if script_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            raise ValueError("Node 调试目前只支持 .js、.mjs 和 .cjs 文件")
        if not script_path.is_file():
            raise FileNotFoundError(f"Node 调试脚本不存在: {normalized}")
        relative_path = script_path.relative_to(self._workspace_root).as_posix()
        return script_path, relative_path

    @staticmethod
    def _normalize_args(args: list[str]) -> list[str]:
        if len(args) > 20:
            raise ValueError("Node 调试参数最多 20 个")
        for argument in args:
            if not isinstance(argument, str):
                raise TypeError("Node 调试参数必须全部是字符串")
        return args

    @staticmethod
    def _positive_int(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"源码断点 {name} 必须是正整数: {value!r}")
        return value

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _remote_value(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        if "value" in value:
            return json.dumps(value["value"], ensure_ascii=False)
        for key in ("unserializableValue", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        return None

    @staticmethod
    def _remote_type(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("type")
        return candidate if isinstance(candidate, str) else None

    @staticmethod
    def _remote_description(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("description")
        return candidate if isinstance(candidate, str) else None

    @staticmethod
    def _remote_object_id(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        candidate = value.get("objectId")
        return candidate if isinstance(candidate, str) else None

    @classmethod
    def _exception_message(cls, value: dict[str, object]) -> str:
        details = value.get("exception")
        return cls._remote_description(details) or "表达式求值失败"

    def _parse_call_frames(
        self,
        runtime: _NodeDebugRuntime,
        value: object,
    ) -> list[NodeDebugStackFrameDTO]:
        if not isinstance(value, list):
            return []
        frames: list[NodeDebugStackFrameDTO] = []
        for raw_frame in value:
            if not isinstance(raw_frame, dict):
                continue
            location = raw_frame.get("location")
            if not isinstance(location, dict):
                continue
            raw_url = raw_frame.get("url")
            url = raw_url if isinstance(raw_url, str) else ""
            line = location.get("lineNumber")
            column = location.get("columnNumber")
            call_frame_id = raw_frame.get("callFrameId")
            if not isinstance(call_frame_id, str) or not isinstance(line, int):
                continue
            if not url:
                script_id = location.get("scriptId")
                if isinstance(script_id, str):
                    url = runtime.script_urls.get(script_id, "")
            frames.append(
                NodeDebugStackFrameDTO(
                    call_frame_id=call_frame_id,
                    function_name=str(raw_frame.get("functionName") or "<anonymous>"),
                    url=url,
                    path=self._url_to_workspace_path(url),
                    line=line + 1,
                    column=(column if isinstance(column, int) else 0) + 1,
                    scope_names=self._scope_names(raw_frame.get("scopeChain")),
                )
            )
        return frames

    @staticmethod
    def _scope_object_ids(value: object) -> dict[str, dict[str, list[str]]]:
        if not isinstance(value, list):
            return {}
        result: dict[str, dict[str, list[str]]] = {}
        for raw_frame in value:
            if not isinstance(raw_frame, dict):
                continue
            call_frame_id = raw_frame.get("callFrameId")
            scope_chain = raw_frame.get("scopeChain")
            if not isinstance(call_frame_id, str) or not isinstance(scope_chain, list):
                continue
            object_ids: dict[str, list[str]] = {
                "local": [],
                "global": [],
            }
            for scope in scope_chain:
                if not isinstance(scope, dict):
                    continue
                scope_object = scope.get("object")
                if not isinstance(scope_object, dict):
                    continue
                object_id = scope_object.get("objectId")
                if isinstance(object_id, str):
                    scope_name = "global" if scope.get("type") == "global" else "local"
                    object_ids[scope_name].append(object_id)
            result[call_frame_id] = object_ids
        return result

    @staticmethod
    def _scope_names(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(scope.get("name"))
            for scope in value
            if isinstance(scope, dict) and isinstance(scope.get("name"), str)
        ]

    def _url_to_workspace_path(self, url: str) -> str | None:
        if not url.startswith("file:"):
            return None
        path = Path(unquote(urlparse(url).path)).resolve()
        try:
            return path.relative_to(self._workspace_root).as_posix()
        except ValueError:
            return None

    def _append_pending_action(
        self,
        session_id: str,
        thread_id: str,
        action: str,
        message: str,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
        result: Literal["success", "error"] = "success",
    ) -> None:
        actions = self._pending_actions.setdefault(
            self._owner_key(session_id, thread_id), []
        )
        append_pending_debug_action(
            actions,
            session_id=session_id,
            thread_id=thread_id,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            extension_catalog_binding=extension_catalog_binding,
            result=result,
            max_actions=_MAX_ACTIONS,
        )

    @staticmethod
    def _append_action(
        runtime: _NodeDebugRuntime,
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
            max_actions=_MAX_ACTIONS,
        )

    def _snapshot(self, runtime: _NodeDebugRuntime) -> NodeDebugStateDTO:
        return build_node_debug_snapshot(
            runtime,
            self._configuration_registry,
        )
