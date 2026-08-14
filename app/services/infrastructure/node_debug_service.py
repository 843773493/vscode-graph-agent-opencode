from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
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
from app.schemas.public_v2.node_debug import (
    NodeDebugAction,
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugBreakpointRequest,
    NodeDebugCapabilitiesDTO,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugEvaluationDTO,
    NodeDebugLaunchProfileDTO,
    NodeDebugSessionManifestDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
    NodeDebugStatus,
    NodeDebugVariableDTO,
)
from app.services.infrastructure.node_debug_breakpoint_expressions import (
    inspector_breakpoint_condition,
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

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug_snapshot import (
    append_pending_debug_action,
    append_runtime_debug_action,
    build_node_debug_snapshot,
)

_INSPECTOR_URL_PATTERN = re.compile(r"Debugger listening on (ws://\S+)")
_SUPPORTED_EXTENSIONS = {".cjs", ".js", ".mjs"}
_MAX_ACTIONS = 100
_MAX_OUTPUT_LINES = 100
_COMMAND_TIMEOUT_SECONDS = 10.0
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


@dataclass(slots=True)
class _NodeDebugRuntime:
    session_id: str
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


class NodeDebugService:
    """通过 Node Inspector 提供会话级 JavaScript 源码调试。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        config_service: ConfigService | None = None,
        session_store: NodeDebugSessionStore | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._config_service = config_service
        self._runtimes: dict[str, _NodeDebugRuntime] = {}
        self._pending_breakpoints: dict[str, list[NodeDebugBreakpointDTO]] = {}
        self._pending_actions: dict[str, list[NodeDebugActionRecordDTO]] = {}
        self._launch_selections: dict[str, _NodeDebugLaunchSelection] = {}
        self._configuration_registry = NodeDebugConfigurationRegistry(
            store=session_store,
            validate_configuration=self._validate_configuration,
        )
        self._runtimes_lock = asyncio.Lock()
        self._node_bin = os.environ.get("BOXTEAM_NODE_BIN") or shutil.which("node")

    async def get_state(self, session_id: str) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        self._configuration_registry.refresh_new_files(session_id)
        runtime = self._runtimes.get(session_id)
        await self._reconcile_session_sources(session_id, runtime)
        if runtime is None:
            selection = self._launch_selections.get(
                session_id,
                _NodeDebugLaunchSelection(),
            )
            return NodeDebugStateDTO(
                session_id=session_id,
                status="idle",
                active_configuration_id=self._configuration_registry.active_id(
                    session_id
                ),
                active_configuration_name=self._configuration_registry.active_name(
                    session_id
                ),
                configurations=self._configuration_registry.summaries(session_id),
                script_path=selection.script_path,
                working_directory=selection.working_directory,
                launch_profile_name=selection.launch_profile_name,
                args=list(selection.args),
                breakpoints=[
                    breakpoint.model_copy(deep=True)
                    for breakpoint in self._pending_breakpoints.get(session_id, [])
                ],
                actions=[
                    action.model_copy(deep=True)
                    for action in self._pending_actions.get(session_id, [])
                ],
                configuration_revision=(
                    self._configuration_registry.active_revision(session_id)
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

    def list_configurations(
        self,
        session_id: str,
    ) -> list[NodeDebugConfigurationDTO]:
        self._ensure_session_loaded(session_id)
        self._configuration_registry.refresh_new_files(session_id)
        return self._configuration_registry.list(session_id)

    def get_configuration(
        self,
        session_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO:
        self._ensure_session_loaded(session_id)
        self._configuration_registry.refresh_new_files(session_id)
        return self._configuration(session_id, configuration_id).model_copy(deep=True)

    async def create_configuration(
        self,
        request: NodeDebugConfigurationCreateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(request.session_id)
        self._configuration_registry.assert_unique_name(
            request.session_id,
            request.name,
        )
        if request.activate:
            self._assert_no_running_target(request.session_id)
        configuration = self._configuration_from_request(
            configuration_id=create_prefixed_id("dbgcfg"),
            name=request.name,
            script_path=request.script_path,
            working_directory=request.working_directory,
            launch_profile_name=request.launch_profile_name,
            args=request.args,
            breakpoints=request.breakpoints,
        )
        self._configuration_registry.put(request.session_id, configuration)
        if request.activate:
            self._activate_configuration_in_memory(
                request.session_id,
                configuration.configuration_id,
            )
        self._record_session_action(
            request.session_id,
            "create_configuration",
            f"已创建调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self._write_session_manifest(request.session_id)
        return await self.get_state(request.session_id)

    async def update_configuration(
        self,
        configuration_id: str,
        request: NodeDebugConfigurationUpdateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(request.session_id)
        current = self._configuration(request.session_id, configuration_id)
        self._assert_configuration_not_running(request.session_id, configuration_id)
        self._configuration_registry.assert_unique_name(
            request.session_id,
            request.name,
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
        self._configuration_registry.put(request.session_id, replacement)
        if (
            self._configuration_registry.active_id(request.session_id)
            == configuration_id
        ):
            self._activate_configuration_in_memory(request.session_id, configuration_id)
        self._record_session_action(
            request.session_id,
            "update_configuration",
            f"已更新调试方案 {replacement.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self._write_session_manifest(request.session_id)
        return await self.get_state(request.session_id)

    async def activate_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        configuration = self._configuration(session_id, configuration_id)
        self._assert_no_running_target(session_id)
        self._activate_configuration_in_memory(session_id, configuration_id)
        self._record_session_action(
            session_id,
            "activate_configuration",
            f"已激活调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self._write_session_manifest(session_id)
        return await self.get_state(session_id)

    async def delete_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        configuration = self._configuration(session_id, configuration_id)
        self._assert_configuration_not_running(session_id, configuration_id)
        self._configuration_registry.remove(session_id, configuration_id)
        if self._configuration_registry.active_id(session_id) == configuration_id:
            self._configuration_registry.clear_active(session_id)
            self._launch_selections.pop(session_id, None)
            self._pending_breakpoints.pop(session_id, None)
            self._runtimes.pop(session_id, None)
        self._record_session_action(
            session_id,
            "delete_configuration",
            f"已删除调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self._write_session_manifest(session_id)
        return await self.get_state(session_id)

    async def import_configuration(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
        *,
        activate: bool = False,
        actor: Literal["human", "ai", "system"] = "human",
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        if activate:
            self._assert_no_running_target(session_id)
        if self._configuration_registry.contains(
            session_id,
            configuration.configuration_id,
        ):
            raise ValueError(
                f"目标会话已存在调试方案: {configuration.configuration_id}"
            )
        self._configuration_registry.assert_unique_name(
            session_id,
            configuration.name,
        )
        imported = self._validate_configuration(configuration)
        self._configuration_registry.put(session_id, imported)
        if activate:
            self._assert_no_running_target(session_id)
            self._activate_configuration_in_memory(
                session_id,
                imported.configuration_id,
            )
        self._record_session_action(
            session_id,
            "import_configuration",
            f"已导入调试方案 {imported.name}",
            actor=actor,
        )
        self._write_session_manifest(session_id)
        return await self.get_state(session_id)

    async def copy_configuration(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        configuration_id: str,
        name: str | None = None,
        activate: bool = False,
    ) -> NodeDebugConfigurationDTO:
        source = self.get_configuration(source_session_id, configuration_id)
        self._ensure_session_loaded(target_session_id)
        if activate:
            self._assert_no_running_target(target_session_id)
        target_name = (name or source.name).strip()
        self._configuration_registry.assert_unique_name(
            target_session_id,
            target_name,
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
        self._configuration_registry.put(target_session_id, copied)
        if activate:
            self._assert_no_running_target(target_session_id)
            self._activate_configuration_in_memory(
                target_session_id,
                copied.configuration_id,
            )
        self._record_session_action(
            target_session_id,
            "copy_configuration",
            f"已从会话 {source_session_id} 复制调试方案 {copied.name}",
            actor="human",
        )
        self._write_session_manifest(target_session_id)
        return copied.model_copy(deep=True)

    async def start(
        self,
        *,
        session_id: str,
        configuration_id: str | None = None,
        path: str,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        launch_profile_name: str | None = None,
        working_directory: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        await self._reconcile_session_sources(
            session_id,
            self._runtimes.get(session_id),
        )
        selected_configuration_id = self._select_configuration_for_start(
            session_id=session_id,
            configuration_id=configuration_id,
            path=path,
            working_directory=working_directory,
            launch_profile_name=launch_profile_name,
            args=args,
        )
        selected_configuration = self._configuration(
            session_id,
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
        pending_breakpoints = list(self._pending_breakpoints.get(session_id, []))
        pending_actions = list(self._pending_actions.get(session_id, []))
        async with self._runtimes_lock:
            previous = self._runtimes.get(session_id)
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
            }:
                await self._stop_runtime(previous)
            runtime = _NodeDebugRuntime(
                session_id=session_id,
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
            self._runtimes[session_id] = runtime
            self._pending_breakpoints.pop(session_id, None)
            self._pending_actions.pop(session_id, None)
            self._launch_selections[session_id] = _NodeDebugLaunchSelection(
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
            self._persist_session_state(session_id, runtime)

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
        try:
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
                runtime.error_message = None
                self._append_action(
                    runtime,
                    "start",
                    "已启动 Node Inspector",
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
            self._persist_session_state(session_id, runtime)
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
            self._persist_session_state(session_id, runtime)
            await self._stop_runtime(runtime, clear_error=False)
            raise RuntimeError(message) from error
        return await self.get_state(session_id)

    async def apply_action(
        self,
        *,
        session_id: str,
        action: NodeDebugAction,
        params: dict[str, object],
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        runtime = self._runtimes.get(session_id)
        await self._reconcile_session_sources(session_id, runtime)
        if runtime is None and action == "set_breakpoint":
            self._ensure_configuration_for_breakpoint(session_id, params)
        if runtime is None and action not in {
            "set_breakpoint",
            "update_breakpoint",
            "clear_breakpoint",
        }:
            raise RuntimeError(f"Node 调试会话不存在: {session_id}")
        if runtime is None:
            params = {**params, "session_id": session_id}
        if action == "set_breakpoint":
            await self._set_breakpoint(
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "update_breakpoint":
            await self._update_breakpoint(
                runtime,
                params,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        elif action == "clear_breakpoint":
            await self._clear_breakpoint(
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
            await self._stop_runtime(runtime)
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
        else:
            await self._debugger_command(
                runtime,
                action,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        self._persist_session_state(session_id, runtime)
        return await self.get_state(session_id)

    async def restart(
        self,
        session_id: str,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            raise RuntimeError(f"Node 调试会话不存在: {session_id}")
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
        await self._stop_runtime(runtime)
        async with runtime.state_lock:
            runtime.status = "exited"
        return await self.start(
            session_id=session_id,
            configuration_id=configuration_id,
            path=path,
            args=args,
            breakpoints=breakpoints,
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
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            removed = len(self._pending_breakpoints.get(session_id, []))
            self._pending_breakpoints.pop(session_id, None)
            self._append_pending_action(
                session_id,
                "clear_all_breakpoints",
                f"已清除全部源码断点（{removed} 个）",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._persist_session_state(session_id, None)
            return await self.get_state(session_id)
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
        self._persist_session_state(session_id, runtime)
        return await self.get_state(session_id)

    async def record_tool_action(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_call_id: str,
        result: Literal["success", "error"],
        message: str,
    ) -> NodeDebugStateDTO:
        self._ensure_session_loaded(session_id)
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            pending = self._pending_actions.setdefault(session_id, [])
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
                    update={"message": message, "result": result, "actor": "ai"}
                )
            else:
                self._append_pending_action(
                    session_id,
                    tool_name,
                    message,
                    actor="ai",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=result,
                )
            self._persist_session_state(session_id, None)
            return await self.get_state(session_id)
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
                    update={"message": message, "result": result, "actor": "ai"}
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
                        result=result,
                    )
        self._persist_session_state(session_id, runtime)
        return await self.get_state(session_id)

    async def get_variables(
        self,
        *,
        session_id: str,
        variable_names: list[str] | None = None,
        scope: str = "all",
    ) -> list[NodeDebugVariableDTO]:
        state = await self.get_state(session_id)
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
            await self._stop_runtime(runtime)

    def _ensure_session_loaded(self, session_id: str) -> None:
        manifest = self._configuration_registry.ensure_loaded(session_id)
        if manifest is None:
            return
        self._pending_actions[session_id] = [
            action.model_copy(deep=True) for action in manifest.actions[-_MAX_ACTIONS:]
        ]
        if manifest.active_configuration_id is not None:
            self._load_active_configuration(session_id)

    def _persist_session_state(
        self,
        session_id: str,
        runtime: _NodeDebugRuntime | None,
    ) -> None:
        configuration_id = self._configuration_registry.active_id(session_id)
        selection = self._launch_selections.get(
            session_id,
            _NodeDebugLaunchSelection(),
        )
        if runtime is not None:
            configuration_id = runtime.configuration_id
            self._configuration_registry.set_active(session_id, configuration_id)
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
            self._launch_selections[session_id] = selection
            breakpoints = [
                persistable_breakpoint(breakpoint)
                for breakpoint in runtime.breakpoints.values()
            ]
            self._pending_actions[session_id] = [
                action.model_copy(deep=True)
                for action in runtime.actions[-_MAX_ACTIONS:]
            ]
        else:
            breakpoints = [
                persistable_breakpoint(breakpoint)
                for breakpoint in self._pending_breakpoints.get(session_id, [])
            ]
            self._pending_actions.setdefault(session_id, [])
        if configuration_id is not None:
            current = self._configuration(session_id, configuration_id)
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
                self._configuration_registry.put(session_id, configuration)
        self._write_session_manifest(session_id)

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
        configuration_id: str | None,
        path: str,
        working_directory: str | None,
        launch_profile_name: str | None,
        args: list[str],
    ) -> str:
        selected_id = configuration_id or self._configuration_registry.active_id(
            session_id
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
            self._configuration_registry.put(session_id, configuration)
            selected_id = configuration.configuration_id
        self._configuration(session_id, selected_id)
        if self._configuration_registry.active_id(session_id) != selected_id:
            self._activate_configuration_in_memory(session_id, selected_id)
        return selected_id

    def _ensure_configuration_for_breakpoint(
        self,
        session_id: str,
        params: dict[str, object],
    ) -> None:
        if self._configuration_registry.active_id(session_id) is not None:
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
        self._configuration_registry.put(session_id, configuration)
        self._activate_configuration_in_memory(
            session_id,
            configuration.configuration_id,
        )

    def _activate_configuration_in_memory(
        self,
        session_id: str,
        configuration_id: str,
    ) -> None:
        self._configuration(session_id, configuration_id)
        runtime = self._runtimes.get(session_id)
        if runtime is not None and runtime.status not in {
            "starting",
            "running",
            "paused",
        }:
            self._runtimes.pop(session_id, None)
        self._configuration_registry.set_active(session_id, configuration_id)
        self._load_active_configuration(session_id)

    def _load_active_configuration(self, session_id: str) -> None:
        configuration_id = self._configuration_registry.active_id(session_id)
        if configuration_id is None:
            raise RuntimeError(f"会话没有活动调试方案: {session_id}")
        configuration = self._configuration(session_id, configuration_id)
        self._launch_selections[session_id] = _NodeDebugLaunchSelection(
            script_path=configuration.script_path,
            working_directory=configuration.working_directory,
            launch_profile_name=configuration.launch_profile_name,
            args=list(configuration.args),
        )
        self._pending_breakpoints[session_id] = [
            persistable_breakpoint(runtime_breakpoint(breakpoint))
            for breakpoint in configuration.breakpoints
        ]

    def _configuration(
        self,
        session_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO:
        return self._configuration_registry.get(session_id, configuration_id)

    def _write_session_manifest(self, session_id: str) -> None:
        self._configuration_registry.write_manifest(
            NodeDebugSessionManifestDTO(
                session_id=session_id,
                active_configuration_id=self._configuration_registry.active_id(
                    session_id
                ),
                actions=[
                    action.model_copy(deep=True)
                    for action in self._pending_actions.get(session_id, [])[
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
        actor: Literal["human", "ai", "system"],
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            self._append_action(
                runtime,
                action,
                message,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._pending_actions[session_id] = [
                item.model_copy(deep=True) for item in runtime.actions[-_MAX_ACTIONS:]
            ]
            return
        self._append_pending_action(
            session_id,
            action,
            message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

    def _assert_no_running_target(self, session_id: str) -> None:
        runtime = self._runtimes.get(session_id)
        if runtime is not None and runtime.status in {"starting", "running", "paused"}:
            raise RuntimeError("目标程序运行中，停止后才能切换调试方案")

    def _assert_configuration_not_running(
        self,
        session_id: str,
        configuration_id: str,
    ) -> None:
        runtime = self._runtimes.get(session_id)
        if (
            runtime is not None
            and runtime.configuration_id == configuration_id
            and runtime.status in {"starting", "running", "paused"}
        ):
            raise RuntimeError("目标程序运行中，不能修改或删除当前调试方案")

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
        runtime: _NodeDebugRuntime | None,
    ) -> None:
        breakpoints = (
            list(runtime.breakpoints.values())
            if runtime is not None
            else list(self._pending_breakpoints.get(session_id, []))
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
            self._pending_breakpoints[session_id] = reconciled
            pending_actions = self._pending_actions.setdefault(session_id, [])
            for message in relocation_messages:
                pending_actions.append(
                    NodeDebugActionRecordDTO(
                        action_id=create_prefixed_id("node-debug-action"),
                        session_id=session_id,
                        action="breakpoint_reconciled",
                        message=message,
                        actor="system",
                        created_at=datetime.now(UTC),
                    )
                )
            del pending_actions[:-_MAX_ACTIONS]

        if should_persist:
            self._persist_session_state(session_id, runtime)

    def _get_debug_runtime_config(self) -> dict[str, object]:
        if self._config_service is None:
            # TODO: 删除无 ConfigService 直连场景的兼容默认值，统一从 workspace 配置读取。
            return {
                "enabled": True,
                "default_adapter": "node_inspector",
                "command_timeout_seconds": _COMMAND_TIMEOUT_SECONDS,
                "node": {
                    "inspector_host": "127.0.0.1",
                    "inspector_port": 0,
                    "executable": "",
                },
                "python": {
                    "adapter": "debugpy",
                    "debugpy_host": "127.0.0.1",
                    "debugpy_port": 0,
                },
                "launch_profiles": {
                    "node-default": {
                        "adapter": "node_inspector",
                        "runtime": "node",
                        "program": "",
                        "working_directory": "",
                        "args": [],
                    }
                },
            }
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
            session_id = str(params.get("session_id") or "")
            pending = self._pending_breakpoints.setdefault(session_id, [])
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
        session_id = (
            runtime.session_id if runtime is not None else params.get("session_id")
        )
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("编辑待启动断点缺少 session_id")
        breakpoints: Iterable[NodeDebugBreakpointDTO] = (
            runtime.breakpoints.values()
            if runtime is not None
            else self._pending_breakpoints.get(session_id, [])
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
            pending = self._pending_breakpoints.get(session_id, [])
            pending[pending.index(current)] = updated
            self._append_pending_action(
                session_id,
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
            session_id = params.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("清除待启动断点缺少 session_id")
            pending = self._pending_breakpoints.get(session_id, [])
            for index, breakpoint in enumerate(pending):
                if breakpoint.breakpoint_id == breakpoint_id:
                    pending.pop(index)
                    if not pending:
                        self._pending_breakpoints.pop(session_id, None)
                    self._append_pending_action(
                        session_id,
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
                        runtime.error_message = None
                    else:
                        runtime.status = "failed"
                        runtime.error_message = "Node Inspector WebSocket 已断开"
                    self._clear_stop_snapshot(runtime)
        except Exception as error:  # noqa: BLE001 - 接收循环必须将适配器故障写入状态
            if not runtime.closing:
                async with runtime.state_lock:
                    runtime.status = "failed"
                    runtime.error_message = f"读取 Node Inspector 事件失败: {error}"
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
                runtime.error_message = None
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
                runtime.error_message = None
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
                runtime.error_message = None
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
        if runtime.closing:
            return
        async with runtime.state_lock:
            runtime.status = "exited" if return_code == 0 else "failed"
            self._clear_stop_snapshot(runtime)
            runtime.error_message = (
                None
                if return_code == 0
                else f"Node 调试进程退出，退出码: {return_code}"
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
    ) -> None:
        runtime.closing = True
        socket = runtime.socket
        if socket is not None:
            await socket.close()
            runtime.socket = None
        process = runtime.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
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
            if clear_error:
                runtime.error_message = None

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
        action: str,
        message: str,
        *,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        result: Literal["success", "error"] = "success",
    ) -> None:
        actions = self._pending_actions.setdefault(session_id, [])
        append_pending_debug_action(
            actions,
            session_id=session_id,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
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
        result: Literal["success", "error"] = "success",
    ) -> None:
        append_runtime_debug_action(
            runtime,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
            max_actions=_MAX_ACTIONS,
        )

    def _snapshot(self, runtime: _NodeDebugRuntime) -> NodeDebugStateDTO:
        return build_node_debug_snapshot(
            runtime,
            self._configuration_registry,
        )
