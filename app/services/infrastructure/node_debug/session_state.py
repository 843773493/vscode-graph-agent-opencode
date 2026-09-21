from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugActionRecordDTO,
    NodeDebugBreakpointDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
)


def _owner(session_id: str, thread_id: str) -> NodeDebugOwner:
    return normalize_node_debug_owner(session_id, thread_id)


_MAX_ACTIONS = 100


class NodeDebugSessionState:
    """集中保存待安装断点和 manifest 动作历史。

    这些状态属于 SessionThread 运行投影，不属于配置文件 registry。所有读写都
    通过本类的行为方法进行，调用方不会拿到可变 mapping，也不会建立第二份缓存。
    """

    def __init__(self, *, configuration_registry: NodeDebugConfigurationRegistry) -> None:
        self._configuration_registry = configuration_registry
        self._pending_breakpoints: dict[NodeDebugOwner, list[NodeDebugBreakpointDTO]] = {}
        self._pending_actions: dict[NodeDebugOwner, list[NodeDebugActionRecordDTO]] = {}

    def ensure_loaded(
        self, session_id: str, thread_id: str
    ) -> NodeDebugSessionManifestDTO | None:
        manifest = self._configuration_registry.ensure_loaded(session_id, thread_id)
        if manifest is not None:
            self.set_pending_actions(
                _owner(session_id, thread_id),
                manifest.actions,
            )
            self.sync_active_configuration(session_id, thread_id)
        return manifest

    def sync_active_configuration(self, session_id: str, thread_id: str) -> None:
        self.set_pending_breakpoints(
            (session_id, thread_id),
            self._configuration_registry.active_breakpoints(session_id, thread_id),
        )

    def pending_breakpoints(
        self, owner: NodeDebugOwner
    ) -> list[NodeDebugBreakpointDTO]:
        return [
            breakpoint.model_copy(deep=True)
            for breakpoint in self._pending_breakpoints.get(owner, [])
        ]

    def set_pending_breakpoints(
        self,
        owner: NodeDebugOwner,
        breakpoints: Iterable[NodeDebugBreakpointDTO],
    ) -> None:
        self._pending_breakpoints[owner] = [
            breakpoint.model_copy(deep=True) for breakpoint in breakpoints
        ]

    def append_pending_breakpoint(
        self, owner: NodeDebugOwner, breakpoint: NodeDebugBreakpointDTO
    ) -> None:
        self._pending_breakpoints.setdefault(owner, []).append(
            breakpoint.model_copy(deep=True)
        )

    def find_pending_breakpoint(
        self, owner: NodeDebugOwner, breakpoint_id: str
    ) -> NodeDebugBreakpointDTO | None:
        return next(
            (
                breakpoint.model_copy(deep=True)
                for breakpoint in self._pending_breakpoints.get(owner, [])
                if breakpoint.breakpoint_id == breakpoint_id
            ),
            None,
        )

    def replace_pending_breakpoint(
        self,
        owner: NodeDebugOwner,
        breakpoint_id: str,
        replacement: NodeDebugBreakpointDTO,
    ) -> None:
        breakpoints = self._pending_breakpoints.get(owner)
        if breakpoints is None:
            raise ValueError(f"源码断点不存在: {breakpoint_id}")
        for index, breakpoint in enumerate(breakpoints):
            if breakpoint.breakpoint_id == breakpoint_id:
                breakpoints[index] = replacement.model_copy(deep=True)
                return
        raise ValueError(f"源码断点不存在: {breakpoint_id}")

    def remove_pending_breakpoint(
        self, owner: NodeDebugOwner, breakpoint_id: str
    ) -> NodeDebugBreakpointDTO:
        breakpoints = self._pending_breakpoints.get(owner, [])
        for index, breakpoint in enumerate(breakpoints):
            if breakpoint.breakpoint_id == breakpoint_id:
                removed = breakpoints.pop(index)
                if not breakpoints:
                    self._pending_breakpoints.pop(owner, None)
                return removed.model_copy(deep=True)
        raise ValueError(f"源码断点不存在: {breakpoint_id}")

    def clear_pending_breakpoints(self, owner: NodeDebugOwner) -> int:
        return len(self._pending_breakpoints.pop(owner, []))

    def consume_pending_breakpoints(
        self, owner: NodeDebugOwner
    ) -> list[NodeDebugBreakpointDTO]:
        breakpoints = self.pending_breakpoints(owner)
        self._pending_breakpoints.pop(owner, None)
        return breakpoints

    def pending_actions(self, owner: NodeDebugOwner) -> list[NodeDebugActionRecordDTO]:
        return [
            action.model_copy(deep=True)
            for action in self._pending_actions.get(owner, [])
        ]

    def set_pending_actions(
        self,
        owner: NodeDebugOwner,
        actions: Iterable[NodeDebugActionRecordDTO],
    ) -> None:
        copied = [action.model_copy(deep=True) for action in actions]
        self._pending_actions[owner] = copied[-_MAX_ACTIONS:]

    def replace_pending_action(
        self,
        owner: NodeDebugOwner,
        index: int,
        replacement: NodeDebugActionRecordDTO,
    ) -> None:
        actions = self._pending_actions.setdefault(owner, [])
        actions[index] = replacement.model_copy(deep=True)

    def append_pending_action(
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
        owner = _owner(session_id, thread_id)
        actions = self._pending_actions.setdefault(owner, [])
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
        del actions[:-_MAX_ACTIONS]

    def consume_pending_actions(
        self, owner: NodeDebugOwner
    ) -> list[NodeDebugActionRecordDTO]:
        actions = self.pending_actions(owner)
        self._pending_actions.pop(owner, None)
        return actions

    def write_session_manifest(self, session_id: str, thread_id: str) -> None:
        self._configuration_registry.write_session_manifest(
            session_id,
            thread_id,
            actions=self.pending_actions(_owner(session_id, thread_id)),
        )

    def persist_runtime_state(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        self._configuration_registry.persist_runtime_state(
            session_id,
            thread_id,
            runtime,
            pending_breakpoints=self.pending_breakpoints(
                _owner(session_id, thread_id)
            ),
        )
        self.write_session_manifest(session_id, thread_id)


__all__ = ["NodeDebugSessionState"]
