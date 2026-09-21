from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugActionRecordDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime

if TYPE_CHECKING:
    from app.services.infrastructure.node_debug.configuration_registry import (
        NodeDebugConfigurationRegistry,
    )

MAX_NODE_DEBUG_ACTIONS = 100


def append_pending_debug_action(
    actions: list[NodeDebugActionRecordDTO],
    *,
    session_id: str,
    thread_id: str,
    action: str,
    message: str,
    actor: Literal["human", "ai", "system"],
    tool_name: str | None,
    tool_call_id: str | None,
    result: Literal["success", "error"],
    max_actions: int,
    extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
) -> None:
    actions.append(
        NodeDebugActionRecordDTO(
            action_id=create_prefixed_id("node-debug-action"),
            session_id=session_id,
            thread_id=thread_id,
            action=action,
            message=message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            extension_catalog_binding=extension_catalog_binding,
            result=result,
            created_at=datetime.now(UTC),
        )
    )
    del actions[:-max_actions]


def append_runtime_debug_action(
    runtime: NodeDebugRuntime,
    *,
    action: str,
    message: str,
    actor: Literal["human", "ai", "system"],
    tool_name: str | None,
    tool_call_id: str | None,
    result: Literal["success", "error"],
    max_actions: int,
    extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None,
) -> None:
    append_pending_debug_action(
        runtime.actions,
        session_id=runtime.session_id,
        thread_id=runtime.thread_id,
        action=action,
        message=message,
        actor=actor,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        extension_catalog_binding=extension_catalog_binding,
        result=result,
        max_actions=max_actions,
    )


def build_node_debug_snapshot(
    runtime: NodeDebugRuntime,
    registry: NodeDebugConfigurationRegistry,
) -> NodeDebugStateDTO:
    process_id = runtime.process.pid if runtime.process is not None else None
    return NodeDebugStateDTO(
        session_id=runtime.session_id,
        # 运行状态必须携带实际 thread 归属；child thread 的状态不能伪装成 main。
        thread_id=runtime.thread_id,
        status=runtime.status,
        active_configuration_id=runtime.configuration_id,
        active_configuration_name=registry.active_name(
            runtime.session_id, runtime.thread_id
        ),
        configurations=registry.summaries(runtime.session_id, runtime.thread_id),
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
        configuration_revision=registry.active_revision(
            runtime.session_id, runtime.thread_id
        ),
        requires_restart=runtime.requires_restart,
        source_changed_paths=sorted(runtime.source_changed_paths),
    )


__all__ = [
    "MAX_NODE_DEBUG_ACTIONS",
    "append_pending_debug_action",
    "append_runtime_debug_action",
    "build_node_debug_snapshot",
]
