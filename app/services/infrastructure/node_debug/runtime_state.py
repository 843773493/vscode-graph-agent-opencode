from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugActionRecordDTO,
    NodeDebugBindingSnapshotDTO,
    NodeDebugBreakpointDTO,
    NodeDebugEvaluationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStatus,
    NodeDebugVariableDTO,
)

#: 仍在推进、尚未进入可核实终态的运行时状态。启动/停止/冲突断言共用同一定义。
ACTIVE_NODE_DEBUG_RUNTIME_STATUSES: frozenset[NodeDebugStatus] = frozenset(
    {"starting", "running", "paused", "stopping", "reconcile_required", "execution_lost"}
)
#: 进程可控制的活体状态（不含停止中与待核实）。
LIVE_NODE_DEBUG_RUNTIME_STATUSES: frozenset[NodeDebugStatus] = frozenset(
    {"starting", "running", "paused"}
)
@dataclass(slots=True)
class NodeDebugInspectorState:
    """单个调试运行时的 Inspector 连接、命令和暂停上下文。"""

    socket: NodeDebugInspectorTransport | None = None
    script_urls: dict[str, str] = field(default_factory=dict)
    scope_object_ids: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    next_command_id: int = 1
    pending_commands: dict[int, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict
    )
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    receiver_task: asyncio.Task[None] | None = None
    variable_hydration_task: asyncio.Task[None] | None = None
    variables_by_scope: dict[str, dict[str, list[NodeDebugVariableDTO]]] = field(default_factory=dict)


@dataclass(slots=True)
class NodeDebugRuntime:
    """Node Debug 各基础设施模块共享的唯一运行时状态。"""

    session_id: str
    thread_id: str
    configuration_id: str
    workspace_root: Path
    script_path: Path
    relative_script_path: str
    run_id: str = field(default_factory=lambda: create_prefixed_id("op"))
    debugger_binding: NodeDebugBindingSnapshotDTO | None = None
    args: list[str] = field(default_factory=list)
    working_directory: Path | None = None
    launch_profile_name: str | None = None
    command_timeout_seconds: float = 10.0
    plugin_runtime: NodeDebugPluginRuntimeProtocol | None = None
    target_pid: int | None = None
    target_exit_code: int | None = None
    # 信号退出可能没有退出码；只由原插件的退出事件或状态回执确认。
    target_exited: bool = False
    status: NodeDebugStatus = "starting"
    #: 本次启动唯一的 process instance 身份；旧 generation 的回调不得写新实例。
    process_instance_id: str | None = None
    paused_reason: str | None = None
    paused_breakpoint_ids: set[str] = field(default_factory=set)
    error_message: str | None = None
    call_stack: list[NodeDebugStackFrameDTO] = field(default_factory=list)
    last_stopped_frame: NodeDebugStackFrameDTO | None = None
    inspector: NodeDebugInspectorState = field(default_factory=NodeDebugInspectorState)
    breakpoints: dict[str, NodeDebugBreakpointDTO] = field(default_factory=dict)
    inspector_breakpoint_ids: dict[str, str] = field(default_factory=dict)
    output: list[str] = field(default_factory=list)
    logpoint_error_message: str | None = None
    last_evaluation: NodeDebugEvaluationDTO | None = None
    evaluations: list[NodeDebugEvaluationDTO] = field(default_factory=list)
    actions: list[NodeDebugActionRecordDTO] = field(default_factory=list)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    process_task: asyncio.Task[None] | None = None
    loaded_source_digests: dict[str, str | None] = field(default_factory=dict)
    requires_restart: bool = False
    source_changed_paths: set[str] = field(default_factory=set)
    closing: bool = False
    dap_subscribers: set[asyncio.Queue[dict[str, object]]] = field(default_factory=set)
    pause_generation: int = 0
    configuration_done: bool = False


def publish_dap_event(runtime: NodeDebugRuntime, event: str, body: dict[str, object]) -> None:
    message = {"event": event, "body": body}
    for subscriber in tuple(runtime.dap_subscribers):
        subscriber.put_nowait(message)


class NodeDebugCommandSender(Protocol):
    async def __call__(
        self,
        runtime: NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


class NodeDebugInspectorTransport(Protocol):
    async def send(self, raw: str) -> None: ...
    def messages(self) -> AsyncIterator[dict[str, object]]: ...
    async def close(self) -> None: ...


class NodeDebugPluginRuntimeProtocol(NodeDebugInspectorTransport, Protocol):
    exit_code: int | None
    pid: int | None
    process_instance_id: str | None

    async def launch(self, payload: dict[str, object]) -> dict[str, object]: ...
    async def stop(self) -> dict[str, object]: ...
    async def status(self) -> dict[str, object]: ...
    def events(self) -> AsyncIterator[dict[str, object]]: ...
    async def close(self) -> None: ...


class NodeDebugActionAppender(Protocol):
    def __call__(
        self,
        runtime: NodeDebugRuntime,
        action: str,
        message: str,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
        result: Literal["success", "error"] = "success",
    ) -> None: ...


class NodeDebugPendingActionAppender(Protocol):
    def __call__(
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
    ) -> None: ...


class NodeDebugStopSnapshotClearer(Protocol):
    def __call__(self, runtime: NodeDebugRuntime) -> None: ...


class NodeDebugLaunchClaimReader(Protocol):
    def __call__(
        self, session_id: str, thread_id: str
    ) -> NodeDebugLaunchClaimDTO | None: ...


class NodeDebugLaunchClaimWriter(Protocol):
    async def __call__(self, claim: NodeDebugLaunchClaimDTO) -> None: ...


class NodeDebugClaimPhaseMarker(Protocol):
    async def __call__(
        self,
        runtime: NodeDebugRuntime,
        phase: Literal["stopping", "execution_lost", "settled"],
        reason: str | None = None,
    ) -> None: ...


class NodeDebugProcessLeaseSettler(Protocol):
    async def __call__(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None: ...


class NodeDebugSessionManifestWriter(Protocol):
    def __call__(self, session_id: str, thread_id: str) -> None: ...


__all__ = [
    "ACTIVE_NODE_DEBUG_RUNTIME_STATUSES",
    "LIVE_NODE_DEBUG_RUNTIME_STATUSES",
    "NodeDebugActionAppender",
    "NodeDebugClaimPhaseMarker",
    "NodeDebugCommandSender",
    "NodeDebugInspectorState",
    "NodeDebugLaunchClaimReader",
    "NodeDebugLaunchClaimWriter",
    "NodeDebugPendingActionAppender",
    "NodeDebugProcessLeaseSettler",
    "NodeDebugRuntime",
    "NodeDebugSessionManifestWriter",
    "NodeDebugStopSnapshotClearer",
]
