from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.core.failures import failure_detail
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugBindingSnapshotDTO,
    NodeDebugBreakpointDTO,
    NodeDebugBreakpointRequest,
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.node_debug.breakpoint.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugLaunchSelection,
)
from app.services.infrastructure.node_debug.configuration.runtime_config import (
    NodeDebugRuntimeConfig,
)
from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.process.launch_claim import (
    claim_with_target_pid,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process.plugin_runtime import (
    NodeDebugPluginRuntime,
)
from app.services.infrastructure.node_debug.process.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugLaunchClaimWriter,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)
from app.services.infrastructure.node_debug.session.thread_owner import NodeDebugOwner


@dataclass(frozen=True, slots=True)
class NodeDebugLaunchRequest:
    owner: NodeDebugOwner
    path: str
    args: list[str]
    breakpoints: list[NodeDebugBreakpointRequest]
    configuration_id: str | None
    launch_profile_name: str | None
    working_directory: str | None
    actor: Literal["human", "ai", "system"]
    tool_name: str | None
    tool_call_id: str | None
    debugger_registration_id: str | None = None
    defer_configuration_done: bool = False
    debugger_binding: NodeDebugBindingSnapshotDTO | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class NodeDebugLaunchResult:
    owner: NodeDebugOwner
    runtime: NodeDebugRuntime


class NodeDebugConfigurationSelector(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        thread_id: str,
        configuration_id: str | None,
        path: str,
        working_directory: str | None,
        launch_profile_name: str | None,
        args: list[str],
    ) -> str: ...


class NodeDebugLaunchConfigReader(Protocol):
    def __call__(self) -> NodeDebugRuntimeConfig: ...


class NodeDebugSourceReconciler(Protocol):
    async def __call__(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None: ...


class NodeDebugLaunchProcessMonitor(Protocol):
    async def __call__(self, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugLaunchTerminalErrorMessage(Protocol):
    def __call__(
        self, runtime: NodeDebugRuntime, return_code: int
    ) -> str | None: ...


class NodeDebugLaunchHandshakeTimeoutMessage(Protocol):
    def __call__(
        self, timeout_seconds: float
    ) -> str: ...


class NodeDebugLaunchSessionLoader(Protocol):
    def __call__(
        self, session_id: str, thread_id: str
    ) -> NodeDebugSessionManifestDTO | None: ...


class NodeDebugLaunchConfigurationReader(Protocol):
    def __call__(
        self,
        session_id: str,
        thread_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO: ...


class NodeDebugLaunchSourceDigestReader(Protocol):
    def __call__(self, runtime: NodeDebugRuntime) -> dict[str, str | None]: ...


class NodeDebugLaunchStatePersister(Protocol):
    def __call__(self, session_id: str, thread_id: str, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugLaunchSelectionWriter(Protocol):
    def __call__(
        self, owner: NodeDebugOwner, selection: NodeDebugLaunchSelection
    ) -> None: ...


class NodeDebugLaunchClaimMarker(Protocol):
    async def __call__(self, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugPausedBreakpointChecker(Protocol):
    def __call__(self, runtime: NodeDebugRuntime) -> bool: ...


@dataclass(slots=True)
class NodeDebugLaunchContext:
    workspace_root: Path
    workspace_id: str | None
    gateway_connection: GatewayTransportConnection
    configuration_factory: NodeDebugConfigurationFactory
    breakpoint_mutations: NodeDebugBreakpointMutations
    inspector: NodeDebugInspector
    lifecycle: NodeDebugProcessLifecycle
    runtimes: MutableMapping[NodeDebugOwner, NodeDebugRuntime]
    session_state: NodeDebugSessionState
    set_selection: NodeDebugLaunchSelectionWriter
    runtimes_lock: asyncio.Lock
    max_actions: int
    load_session: NodeDebugLaunchSessionLoader
    reconcile_sources: NodeDebugSourceReconciler
    select_configuration: NodeDebugConfigurationSelector
    read_configuration: NodeDebugLaunchConfigurationReader
    read_runtime_config: NodeDebugLaunchConfigReader
    read_source_digests: NodeDebugLaunchSourceDigestReader
    persist_state: NodeDebugLaunchStatePersister
    write_claim: NodeDebugLaunchClaimWriter
    mark_claim_running: NodeDebugLaunchClaimMarker
    append_action: NodeDebugActionAppender
    is_paused_at_breakpoint: NodeDebugPausedBreakpointChecker
    monitor_process: NodeDebugLaunchProcessMonitor
    terminal_error_message: NodeDebugLaunchTerminalErrorMessage


class NodeDebugLaunchOrchestrator:
    """按单一顺序编排 Node 调试启动：配置、claim、spawn、握手和持久化。"""

    def __init__(self, context: NodeDebugLaunchContext) -> None:
        self._context = context

    async def launch(self, request: NodeDebugLaunchRequest) -> NodeDebugLaunchResult:
        context = self._context
        session_id, thread_id = request.owner
        context.load_session(session_id, thread_id)
        await context.lifecycle.assert_claim_recoverable(request.owner)
        await context.reconcile_sources(
            session_id,
            thread_id,
            context.runtimes.get(request.owner),
        )
        selected_configuration_id = context.select_configuration(
            session_id=session_id,
            thread_id=thread_id,
            configuration_id=request.configuration_id,
            path=request.path,
            working_directory=request.working_directory,
            launch_profile_name=request.launch_profile_name,
            args=request.args,
        )
        context.session_state.sync_active_configuration(session_id, thread_id)
        selected_configuration = context.read_configuration(
            session_id,
            thread_id,
            selected_configuration_id,
        )
        if selected_configuration.script_path is None:
            raise ValueError(f"调试方案没有目标文件: {selected_configuration.name}")

        runtime_config = context.read_runtime_config()
        if not runtime_config.enabled:
            raise RuntimeError("源码调试能力未启用: runtime.debug.enabled=false")
        profile_name, profile = runtime_config.resolve_profile(
            selected_configuration.launch_profile_name
        )
        if profile.adapter != "node_inspector":
            raise RuntimeError(
                f"当前版本不支持调试 adapter: {profile.adapter}; 仅支持 node_inspector"
            )
        if profile.runtime != "node":
            raise RuntimeError(
                f"Node Inspector profile 的 runtime 必须是 node: {profile.runtime!r}"
            )
        resolved_working_directory = context.configuration_factory.resolve_working_directory(
            selected_configuration.working_directory or profile.working_directory
        )
        script_path, relative_path = context.configuration_factory.resolve_script_path(
            selected_configuration.script_path
        )
        normalized_args = context.configuration_factory.normalize_args(
            selected_configuration.args or profile.args
        )
        node_bin = runtime_config.node.executable.strip() or "node"

        pending_breakpoints = context.session_state.pending_breakpoints(request.owner)
        pending_actions = context.session_state.pending_actions(request.owner)
        async with context.runtimes_lock:
            previous = context.runtimes.get(request.owner)
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
                        for action in previous.actions[-context.max_actions :]
                    ]
            if previous is not None:
                previous_outcome = await context.lifecycle.stop_runtime(previous)
                if previous_outcome == "reconcile_required":
                    raise RuntimeError(
                        "旧调试实例无法核实终态，已保持 reconcile_required；"
                        "核实并结清前拒绝启动新实例: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
            runtime = NodeDebugRuntime(
                session_id=session_id,
                thread_id=thread_id,
                configuration_id=selected_configuration_id,
                debugger_binding=request.debugger_binding,
                workspace_root=context.workspace_root,
                script_path=script_path,
                relative_script_path=relative_path,
                args=list(normalized_args),
                working_directory=resolved_working_directory,
                launch_profile_name=profile_name,
                command_timeout_seconds=runtime_config.command_timeout_seconds,
            )
            requested_breakpoints = [
                breakpoint.model_copy(deep=True) for breakpoint in pending_breakpoints
            ]
            if previous is not None and previous.configuration_id == selected_configuration_id:
                requested_breakpoints.extend(previous_breakpoints)
            requested_breakpoints.extend(
                context.configuration_factory.create_breakpoint(
                    path=breakpoint.path,
                    line=breakpoint.line,
                    column=breakpoint.column,
                    condition=breakpoint.condition,
                    hit_condition=breakpoint.hit_condition,
                    log_message=breakpoint.log_message,
                )
                for breakpoint in request.breakpoints
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
                    }
                )
                runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint
            source_actions = previous_actions if previous is not None else pending_actions
            runtime.actions.extend(action.model_copy(deep=True) for action in source_actions)
            del runtime.actions[:-context.max_actions]
            context.runtimes[request.owner] = runtime
            context.session_state.consume_pending_breakpoints(request.owner)
            context.session_state.consume_pending_actions(request.owner)
            context.set_selection(request.owner, NodeDebugLaunchSelection(
                script_path=relative_path,
                working_directory=(
                    resolved_working_directory.relative_to(context.workspace_root).as_posix()
                    if resolved_working_directory != context.workspace_root
                    else ""
                ),
                launch_profile_name=profile_name,
                args=list(normalized_args),
            ))
            runtime.loaded_source_digests = context.read_source_digests(runtime)
            context.persist_state(session_id, thread_id, runtime)

        debugger_binding = runtime.debugger_binding
        if debugger_binding is None:
            raise RuntimeError("调试运行必须绑定当前 Gateway Workspace 的 ready Debug 插件")
        claim = new_launch_claim(
            session_id=session_id,
            thread_id=thread_id,
            run_id=runtime.run_id,
            configuration_id=selected_configuration_id,
            debugger_binding=debugger_binding,
            script_path=relative_path,
            args=normalized_args,
            node_executable=node_bin,
            working_directory=(
                resolved_working_directory.relative_to(context.workspace_root).as_posix()
                if resolved_working_directory != context.workspace_root
                else ""
            ),
            launch_profile_name=profile_name,
            breakpoints=list(runtime.breakpoints.values()),
        )
        runtime.process_instance_id = claim.process_instance_id
        try:
            async with runtime.state_lock:
                await context.write_claim(claim)
                if runtime.closing:
                    raise RuntimeError(
                        "并发的停止请求已接管该调试运行时，取消 spawn: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
            if runtime.debugger_binding is None or context.workspace_id is None:
                raise RuntimeError("调试运行必须绑定当前 Gateway Workspace 的 ready Debug 插件")
            plugin_runtime = await NodeDebugPluginRuntime.connect(
                binding=runtime.debugger_binding,
                workspace_id=context.workspace_id,
                connection=context.gateway_connection,
                request_id=request.request_id or create_prefixed_id("req"),
                run_id=runtime.run_id,
            )
            runtime.plugin_runtime = plugin_runtime
            plugin_result = await plugin_runtime.launch({
                "node_executable": node_bin,
                "inspector_host": runtime_config.node.inspector_host,
                "inspector_port": runtime_config.node.inspector_port,
                "script_path": str(script_path),
                "working_directory": str(resolved_working_directory),
                "args": normalized_args,
                "process_instance_id": claim.process_instance_id,
            })
            pid = plugin_result["pid"]
            if type(pid) is not int:
                raise TypeError("Debug 插件 launch 回执 PID 无效")
            runtime.target_pid = pid
            runtime.process_instance_id = claim.process_instance_id
            claim = claim_with_target_pid(claim, pid=pid)
            await context.write_claim(claim)
            runtime.process_task = asyncio.create_task(context.monitor_process(runtime))
            await context.inspector.connect(runtime)
            await context.inspector.command(runtime, "Runtime.enable")
            await context.inspector.command(runtime, "Debugger.enable")
            await context.inspector.command(
                runtime,
                "NodeRuntime.notifyWhenWaitingForDisconnect",
                {"enabled": True},
            )
            await context.mark_claim_running(runtime)
            for breakpoint in runtime.breakpoints.values():
                if breakpoint.relocation_status == "current":
                    await context.breakpoint_mutations.install_breakpoint(runtime, breakpoint)
            async with runtime.state_lock:
                runtime.status = "starting"
            if not request.defer_configuration_done:
                await context.inspector.command(runtime, "Runtime.runIfWaitingForDebugger")
            await context.inspector.wait_for_execution_state(runtime)
            resume_initial_pause = False
            async with runtime.state_lock:
                if (not request.defer_configuration_done and runtime.status == "paused"
                        and not context.is_paused_at_breakpoint(runtime)):
                    runtime.status = "running"
                    context.inspector.clear_paused_snapshot(runtime)
                    runtime.last_stopped_frame = None
                    resume_initial_pause = True
            if resume_initial_pause:
                await context.inspector.command(runtime, "Debugger.resume")
                await context.inspector.wait_for_execution_state(runtime)
            await context.inspector.wait_for_frame_variables(runtime)
            return_code = runtime.target_exit_code
            if return_code is not None and return_code != 0:
                # Debug 插件已确认目标进程退出；输出由同一控制通道保存。
                raise RuntimeError(
                    context.terminal_error_message(runtime, return_code)
                    or f"Node 调试进程在握手期间退出，退出码: {return_code}"
                )
            async with runtime.state_lock:
                if return_code is None and runtime.status not in {
                    "exited",
                    "failed",
                    "paused",
                }:
                    runtime.status = "running"
                runtime.error_message = runtime.logpoint_error_message
                context.append_action(
                    runtime,
                    "start",
                    "已启动 Node Inspector",
                    actor=request.actor,
                    tool_name=request.tool_name,
                    tool_call_id=request.tool_call_id,
                )
            context.persist_state(session_id, thread_id, runtime)
        except Exception as error:
            if isinstance(error, OSError):
                # 原生位置留在诊断及 cause；状态、动作和 manifest 共用同一公开事实。
                message = failure_detail(
                    error, stage="node-debug-launch",
                    label=(
                        "启动 Node Inspector 失败: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    ),
                )
            else:
                message = f"启动 Node Inspector 失败: {error}"
            async with runtime.state_lock:
                runtime.status = "failed"
                runtime.error_message = message
                context.append_action(
                    runtime,
                    "start_failed",
                    message,
                    actor=request.actor,
                    tool_name=request.tool_name,
                    tool_call_id=request.tool_call_id,
                    result="error",
                )
            context.persist_state(session_id, thread_id, runtime)
            await context.lifecycle.stop_runtime(runtime, clear_error=False)
            raise RuntimeError(message) from error
        return NodeDebugLaunchResult(owner=request.owner, runtime=runtime)



__all__ = [
    "NodeDebugLaunchContext",
    "NodeDebugLaunchOrchestrator",
    "NodeDebugLaunchRequest",
    "NodeDebugLaunchResult",
    "NodeDebugLaunchSelection",
]
