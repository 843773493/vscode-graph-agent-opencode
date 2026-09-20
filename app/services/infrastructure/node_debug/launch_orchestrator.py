from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugBreakpointRequest,
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
)
from app.services.infrastructure.node_debug.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.launch_claim import (
    claim_with_spawn_identity,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_config import (
    NodeDebugRuntimeConfig,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.thread_owner import NodeDebugOwner


@dataclass(frozen=True, slots=True)
class NodeDebugLaunchSelection:
    script_path: str | None = None
    working_directory: str | None = None
    launch_profile_name: str | None = None
    args: list[str] = field(default_factory=list)


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


class NodeDebugLaunchStreamReader(Protocol):
    async def __call__(
        self,
        runtime: NodeDebugRuntime,
        stream_name: Literal["stdout", "stderr"],
    ) -> None: ...


class NodeDebugLaunchProcessMonitor(Protocol):
    async def __call__(self, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugLaunchSessionLoader(Protocol):
    def __call__(self, session_id: str, thread_id: str) -> None: ...


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


class NodeDebugLaunchClaimWriter(Protocol):
    def __call__(self, claim: NodeDebugLaunchClaimDTO) -> None: ...


class NodeDebugLaunchClaimMarker(Protocol):
    def __call__(self, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugPausedBreakpointChecker(Protocol):
    def __call__(self, runtime: NodeDebugRuntime) -> bool: ...


@dataclass(slots=True)
class NodeDebugLaunchContext:
    workspace_root: Path
    node_bin: str | None
    configuration_factory: NodeDebugConfigurationFactory
    breakpoint_mutations: NodeDebugBreakpointMutations
    inspector: NodeDebugInspector
    lifecycle: NodeDebugProcessLifecycle
    runtimes: MutableMapping[NodeDebugOwner, NodeDebugRuntime]
    pending_breakpoints: MutableMapping[NodeDebugOwner, list[NodeDebugBreakpointDTO]]
    pending_actions: MutableMapping[NodeDebugOwner, list[NodeDebugActionRecordDTO]]
    launch_selections: MutableMapping[NodeDebugOwner, NodeDebugLaunchSelection]
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
    read_stream: NodeDebugLaunchStreamReader
    monitor_process: NodeDebugLaunchProcessMonitor


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
        configured_node_bin = runtime_config.node.executable.strip()
        node_bin = configured_node_bin or context.node_bin
        if not node_bin:
            raise RuntimeError(
                "未找到 Node.js，可通过 runtime.debug.node.executable 或 "
                "BOXTEAM_NODE_BIN 指定"
            )

        pending_breakpoints = list(context.pending_breakpoints.get(request.owner, []))
        pending_actions = list(context.pending_actions.get(request.owner, []))
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
            if previous is not None and previous.status in {
                "starting",
                "running",
                "paused",
                "stopping",
                "reconcile_required",
            }:
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
                workspace_root=context.workspace_root,
                script_path=script_path,
                relative_script_path=relative_path,
                args=list(normalized_args),
                working_directory=resolved_working_directory,
                launch_profile_name=profile_name,
                node_bin=node_bin,
                inspector_host=runtime_config.node.inspector_host,
                inspector_port=runtime_config.node.inspector_port,
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
                        "inspector_id": None,
                    }
                )
                runtime.breakpoints[breakpoint.breakpoint_id] = breakpoint
            source_actions = previous_actions if previous is not None else pending_actions
            runtime.actions.extend(action.model_copy(deep=True) for action in source_actions)
            del runtime.actions[:-context.max_actions]
            context.runtimes[request.owner] = runtime
            context.pending_breakpoints.pop(request.owner, None)
            context.pending_actions.pop(request.owner, None)
            context.launch_selections[request.owner] = NodeDebugLaunchSelection(
                script_path=relative_path,
                working_directory=(
                    resolved_working_directory.relative_to(context.workspace_root).as_posix()
                    if resolved_working_directory != context.workspace_root
                    else ""
                ),
                launch_profile_name=profile_name,
                args=list(normalized_args),
            )
            runtime.loaded_source_digests = context.read_source_digests(runtime)
            context.persist_state(session_id, thread_id, runtime)

        claim = new_launch_claim(
            session_id=session_id,
            thread_id=thread_id,
            configuration_id=selected_configuration_id,
            inspector_host=runtime.inspector_host,
            inspector_port=runtime.inspector_port,
        )
        runtime.process_instance_id = claim.process_instance_id
        context.write_claim(claim)
        try:
            async with runtime.state_lock:
                if runtime.closing:
                    raise RuntimeError(
                        "并发的停止请求已接管该调试运行时，取消 spawn: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    )
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
            runtime.stderr_task = asyncio.create_task(context.read_stream(runtime, "stderr"))
            runtime.stdout_task = asyncio.create_task(context.read_stream(runtime, "stdout"))
            runtime.process_task = asyncio.create_task(context.monitor_process(runtime))
            spawn_identity = probe_process_identity(runtime.process.pid)
            if spawn_identity is None:
                raise RuntimeError(
                    "spawn 后无法读取进程起始身份（进程可能已立即退出）: "
                    f"pid={runtime.process.pid}"
                )
            runtime.process_identity_source = spawn_identity.source
            runtime.process_start_marker = spawn_identity.start_marker
            context.write_claim(
                claim_with_spawn_identity(
                    claim,
                    pid=runtime.process.pid,
                    identity=spawn_identity,
                )
            )
            await asyncio.wait_for(
                runtime.inspector.inspector_ready.wait(),
                timeout=runtime.command_timeout_seconds,
            )
            await context.inspector.connect(runtime)
            await context.inspector.command(runtime, "Runtime.enable")
            await context.inspector.command(runtime, "Debugger.enable")
            await context.inspector.command(
                runtime,
                "NodeRuntime.notifyWhenWaitingForDisconnect",
                {"enabled": True},
            )
            context.mark_claim_running(runtime)
            for breakpoint in runtime.breakpoints.values():
                if breakpoint.relocation_status == "current":
                    await context.breakpoint_mutations.install_breakpoint(runtime, breakpoint)
            async with runtime.state_lock:
                runtime.status = "starting"
            await context.inspector.command(runtime, "Runtime.runIfWaitingForDebugger")
            await context.inspector.wait_for_execution_state(runtime)
            resume_initial_pause = False
            async with runtime.state_lock:
                if runtime.status == "paused" and not context.is_paused_at_breakpoint(runtime):
                    runtime.status = "running"
                    context.inspector.clear_paused_snapshot(runtime)
                    runtime.last_stopped_frame = None
                    resume_initial_pause = True
            if resume_initial_pause:
                await context.inspector.command(runtime, "Debugger.resume")
                await context.inspector.wait_for_execution_state(runtime)
            await context.inspector.wait_for_frame_variables(runtime)
            async with runtime.state_lock:
                if runtime.status not in {"exited", "failed", "paused"} and (
                    runtime.process is None or runtime.process.returncode is None
                ):
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
