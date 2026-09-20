from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from websockets.asyncio.client import ClientConnection

from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugEvaluationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStatus,
)

_COMMAND_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class NodeDebugInspectorState:
    """单个调试运行时的 Inspector 连接、命令和暂停上下文。"""

    socket: ClientConnection | None = None
    inspector_url: str | None = None
    script_urls: dict[str, str] = field(default_factory=dict)
    scope_object_ids: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    next_command_id: int = 1
    pending_commands: dict[int, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict
    )
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inspector_ready: asyncio.Event = field(default_factory=asyncio.Event)
    receiver_task: asyncio.Task[None] | None = None
    variable_hydration_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class NodeDebugRuntime:
    """Node Debug 各基础设施模块共享的唯一运行时状态。"""

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
    inspector: NodeDebugInspectorState = field(default_factory=NodeDebugInspectorState)
    breakpoints: dict[str, NodeDebugBreakpointDTO] = field(default_factory=dict)
    inspector_breakpoint_ids: dict[str, str] = field(default_factory=dict)
    output: list[str] = field(default_factory=list)
    logpoint_error_message: str | None = None
    last_evaluation: NodeDebugEvaluationDTO | None = None
    evaluations: list[NodeDebugEvaluationDTO] = field(default_factory=list)
    actions: list[NodeDebugActionRecordDTO] = field(default_factory=list)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_task: asyncio.Task[None] | None = None
    stdout_task: asyncio.Task[None] | None = None
    process_task: asyncio.Task[None] | None = None
    loaded_source_digests: dict[str, str | None] = field(default_factory=dict)
    requires_restart: bool = False
    source_changed_paths: set[str] = field(default_factory=set)
    closing: bool = False


class NodeDebugCommandSender(Protocol):
    async def __call__(
        self,
        runtime: NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


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
    def __call__(self, claim: NodeDebugLaunchClaimDTO) -> None: ...


class NodeDebugClaimPhaseMarker(Protocol):
    def __call__(
        self,
        runtime: NodeDebugRuntime,
        phase: Literal["stopping", "reconcile_required", "settled"],
        reason: str | None = None,
    ) -> None: ...


class NodeDebugProcessLeaseSettler(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None: ...


class NodeDebugReleaseFailureNotifier(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None: ...


class NodeDebugSessionManifestWriter(Protocol):
    def __call__(self, session_id: str, thread_id: str) -> None: ...


__all__ = [
    "NodeDebugActionAppender",
    "NodeDebugClaimPhaseMarker",
    "NodeDebugCommandSender",
    "NodeDebugInspectorState",
    "NodeDebugLaunchClaimReader",
    "NodeDebugLaunchClaimWriter",
    "NodeDebugPendingActionAppender",
    "NodeDebugProcessLeaseSettler",
    "NodeDebugReleaseFailureNotifier",
    "NodeDebugRuntime",
    "NodeDebugSessionManifestWriter",
    "NodeDebugStopSnapshotClearer",
]
