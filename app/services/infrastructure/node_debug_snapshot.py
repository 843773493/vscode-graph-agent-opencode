from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from app.core.identifier import create_prefixed_id
from app.schemas.public_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugEvaluationDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
    NodeDebugStatus,
)
from app.services.infrastructure.node_debug_configuration_registry import (
    NodeDebugConfigurationRegistry,
)


class _DebugProcess(Protocol):
    pid: int


class NodeDebugSnapshotRuntime(Protocol):
    session_id: str
    status: NodeDebugStatus
    configuration_id: str
    process: _DebugProcess | None
    workspace_root: Path
    working_directory: Path
    relative_script_path: str
    launch_profile_name: str | None
    args: list[str]
    paused_reason: str | None
    error_message: str | None
    call_stack: list[NodeDebugStackFrameDTO]
    last_stopped_frame: NodeDebugStackFrameDTO | None
    breakpoints: dict[str, NodeDebugBreakpointDTO]
    output: list[str]
    last_evaluation: NodeDebugEvaluationDTO | None
    evaluations: list[NodeDebugEvaluationDTO]
    actions: list[NodeDebugActionRecordDTO]
    requires_restart: bool
    source_changed_paths: set[str]


def append_pending_debug_action(
    actions: list[NodeDebugActionRecordDTO],
    *,
    session_id: str,
    action: str,
    message: str,
    actor: Literal["human", "ai", "system"],
    tool_name: str | None,
    tool_call_id: str | None,
    result: Literal["success", "error"],
    max_actions: int,
) -> None:
    actions.append(
        NodeDebugActionRecordDTO(
            action_id=create_prefixed_id("node-debug-action"),
            session_id=session_id,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
            created_at=datetime.now(UTC),
        )
    )
    del actions[:-max_actions]


def append_runtime_debug_action(
    runtime: NodeDebugSnapshotRuntime,
    *,
    action: str,
    message: str,
    actor: Literal["human", "ai", "system"],
    tool_name: str | None,
    tool_call_id: str | None,
    result: Literal["success", "error"],
    max_actions: int,
) -> None:
    append_pending_debug_action(
        runtime.actions,
        session_id=runtime.session_id,
        action=action,
        message=message,
        actor=actor,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        result=result,
        max_actions=max_actions,
    )


def build_node_debug_snapshot(
    runtime: NodeDebugSnapshotRuntime,
    registry: NodeDebugConfigurationRegistry,
) -> NodeDebugStateDTO:
    process_id = runtime.process.pid if runtime.process is not None else None
    return NodeDebugStateDTO(
        session_id=runtime.session_id,
        status=runtime.status,
        active_configuration_id=runtime.configuration_id,
        active_configuration_name=registry.active_name(runtime.session_id),
        configurations=registry.summaries(runtime.session_id),
        script_path=runtime.relative_script_path,
        working_directory=(
            str(runtime.working_directory.relative_to(runtime.workspace_root))
            if runtime.working_directory != runtime.workspace_root
            else ""
        ),
        launch_profile_name=runtime.launch_profile_name,
        args=list(runtime.args),
        pid=process_id,
        paused_reason=runtime.paused_reason,
        error_message=runtime.error_message,
        call_stack=[frame.model_copy(deep=True) for frame in runtime.call_stack],
        last_stopped_frame=(
            runtime.last_stopped_frame.model_copy(deep=True)
            if runtime.last_stopped_frame is not None
            else None
        ),
        breakpoints=[
            breakpoint.model_copy(deep=True)
            for breakpoint in runtime.breakpoints.values()
        ],
        output=list(runtime.output),
        last_evaluation=(
            runtime.last_evaluation.model_copy(deep=True)
            if runtime.last_evaluation is not None
            else None
        ),
        evaluations=[
            evaluation.model_copy(deep=True) for evaluation in runtime.evaluations
        ],
        actions=[action.model_copy(deep=True) for action in runtime.actions],
        configuration_revision=registry.active_revision(runtime.session_id),
        requires_restart=runtime.requires_restart,
        source_changed_paths=sorted(runtime.source_changed_paths),
    )


__all__ = [
    "append_pending_debug_action",
    "append_runtime_debug_action",
    "build_node_debug_snapshot",
]
