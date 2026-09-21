from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationSummaryDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoints import (
    persistable_breakpoint,
    runtime_breakpoint,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
)


@dataclass(frozen=True, slots=True)
class NodeDebugLaunchSelection:
    """当前 owner 的可复用启动参数投影。"""

    script_path: str | None = None
    working_directory: str | None = None
    launch_profile_name: str | None = None
    args: list[str] = field(default_factory=list)


def _owner(session_id: str, thread_id: str) -> NodeDebugOwner:
    return normalize_node_debug_owner(session_id, thread_id)


class NodeDebugConfigurationRegistry:
    """管理 Node Debug 配置与活动选择。

    配置文件、活动方案和启动选择只在这里保存一份；运行态配置回写与 manifest
    动作历史属于 ``NodeDebugSessionState``，避免把会话运行投影混入配置 registry。
    """

    def __init__(
        self,
        *,
        store: NodeDebugSessionStore | None,
        configuration_factory: NodeDebugConfigurationFactory,
    ) -> None:
        self._store = store
        self._configuration_factory = configuration_factory
        self._configurations: dict[
            NodeDebugOwner, dict[str, NodeDebugConfigurationDTO]
        ] = {}
        self._active_configuration_ids: dict[NodeDebugOwner, str] = {}
        self._launch_selections: dict[NodeDebugOwner, NodeDebugLaunchSelection] = {}
        self._loaded_sessions: set[NodeDebugOwner] = set()

    def ensure_loaded(
        self, session_id: str, thread_id: str
    ) -> NodeDebugSessionManifestDTO | None:
        owner = _owner(session_id, thread_id)
        if owner in self._loaded_sessions:
            return None
        configurations = (
            self._store.list_configurations(session_id, thread_id)
            if self._store is not None
            else []
        )
        by_id: dict[str, NodeDebugConfigurationDTO] = {}
        names: set[str] = set()
        for configuration in configurations:
            validated = self._configuration_factory.validate_configuration(configuration)
            if validated.configuration_id in by_id:
                raise RuntimeError(
                    f"会话存在重复调试方案 ID: {validated.configuration_id}"
                )
            normalized_name = validated.name.casefold()
            if normalized_name in names:
                raise RuntimeError(f"会话存在重复调试方案名称: {validated.name}")
            names.add(normalized_name)
            by_id[validated.configuration_id] = validated
        self._configurations[owner] = by_id
        manifest = (
            self._store.read_manifest(session_id, thread_id)
            if self._store is not None
            else None
        )
        if manifest is not None and manifest.active_configuration_id is not None:
            if manifest.active_configuration_id not in by_id:
                raise RuntimeError(
                    "活动调试方案不存在: "
                    f"session_id={session_id}, thread_id={thread_id}, "
                    f"configuration_id={manifest.active_configuration_id}"
                )
            self._active_configuration_ids[owner] = manifest.active_configuration_id
            self._load_active_configuration(session_id, thread_id)
        self._loaded_sessions.add(owner)
        return manifest

    def refresh_new_files(self, session_id: str, thread_id: str) -> None:
        if self._store is None:
            return
        owner = _owner(session_id, thread_id)
        known = self._configurations.setdefault(owner, {})
        for configuration in self._store.list_configurations(session_id, thread_id):
            if configuration.configuration_id in known:
                continue
            validated = self._configuration_factory.validate_configuration(configuration)
            self.assert_unique_name(session_id, validated.name, thread_id=thread_id)
            known[validated.configuration_id] = validated

    def list(self, session_id: str, thread_id: str) -> list[NodeDebugConfigurationDTO]:
        return [
            configuration.model_copy(deep=True)
            for configuration in sorted(
                self._configurations.get(_owner(session_id, thread_id), {}).values(),
                key=lambda item: (item.name.casefold(), item.configuration_id),
            )
        ]

    def get(
        self, session_id: str, thread_id: str, configuration_id: str
    ) -> NodeDebugConfigurationDTO:
        configuration = self._configurations.get(_owner(session_id, thread_id), {}).get(
            configuration_id
        )
        if configuration is None:
            raise FileNotFoundError(
                "调试方案不存在: "
                f"session_id={session_id}, thread_id={thread_id}, "
                f"configuration_id={configuration_id}"
            )
        return configuration

    def contains(self, session_id: str, thread_id: str, configuration_id: str) -> bool:
        return configuration_id in self._configurations.get(
            _owner(session_id, thread_id), {}
        )

    def put(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
        thread_id: str,
    ) -> None:
        self._configurations.setdefault(_owner(session_id, thread_id), {})[
            configuration.configuration_id
        ] = configuration
        if self._store is not None:
            self._store.write_configuration(session_id, configuration, thread_id)

    def create(self, request: NodeDebugConfigurationCreateRequest) -> NodeDebugConfigurationDTO:
        session_id, thread_id = request.session_id, request.thread_id
        self.ensure_loaded(session_id, thread_id)
        self.assert_unique_name(session_id, request.name, thread_id=thread_id)
        configuration = self._configuration_factory.configuration_from_request(
            configuration_id=create_prefixed_id("dbgcfg"),
            name=request.name,
            script_path=request.script_path,
            working_directory=request.working_directory,
            launch_profile_name=request.launch_profile_name,
            args=request.args,
            breakpoints=request.breakpoints,
        )
        self.put(session_id, configuration, thread_id)
        if request.activate:
            self.activate(session_id, configuration.configuration_id, thread_id=thread_id)
        return configuration

    def update(
        self,
        configuration_id: str,
        request: NodeDebugConfigurationUpdateRequest,
    ) -> NodeDebugConfigurationDTO:
        session_id, thread_id = request.session_id, request.thread_id
        self.ensure_loaded(session_id, thread_id)
        current = self.get(session_id, thread_id, configuration_id)
        self.assert_unique_name(
            session_id,
            request.name,
            thread_id=thread_id,
            exclude_configuration_id=configuration_id,
        )
        replacement = self._configuration_factory.configuration_from_request(
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
        self.put(session_id, replacement, thread_id)
        if self.active_id(session_id, thread_id) == configuration_id:
            self._load_active_configuration(session_id, thread_id)
        return replacement

    def activate(
        self,
        session_id: str,
        configuration_id: str,
        *,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO:
        configuration = self.get(session_id, thread_id, configuration_id)
        self.set_active(session_id, configuration_id, thread_id)
        self._load_active_configuration(session_id, thread_id)
        return configuration

    def remove(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO:
        configuration = self.get(session_id, thread_id, configuration_id)
        owner = _owner(session_id, thread_id)
        del self._configurations[owner][configuration_id]
        if self._store is not None:
            self._store.delete_configuration(session_id, configuration_id, thread_id)
        if self.active_id(session_id, thread_id) == configuration_id:
            self.clear_active(session_id, thread_id)
            self._launch_selections.pop(owner, None)
        return configuration

    def import_configuration(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
        *,
        thread_id: str,
        activate: bool = False,
    ) -> NodeDebugConfigurationDTO:
        self.ensure_loaded(session_id, thread_id)
        if self.contains(session_id, thread_id, configuration.configuration_id):
            raise ValueError(f"目标会话已存在调试方案: {configuration.configuration_id}")
        self.assert_unique_name(session_id, configuration.name, thread_id=thread_id)
        imported = self._configuration_factory.validate_configuration(configuration)
        self.put(session_id, imported, thread_id)
        if activate:
            self.activate(session_id, imported.configuration_id, thread_id=thread_id)
        return imported

    def copy_configuration(
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
        source = self.get(source_session_id, source_thread_id, configuration_id)
        self.ensure_loaded(target_session_id, target_thread_id)
        target_name = (name or source.name).strip()
        self.assert_unique_name(target_session_id, target_name, thread_id=target_thread_id)
        now = datetime.now(UTC)
        copied = self._configuration_factory.validate_configuration(
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
        self.put(target_session_id, copied, target_thread_id)
        if activate:
            self.activate(target_session_id, copied.configuration_id, thread_id=target_thread_id)
        return copied

    def active_id(self, session_id: str, thread_id: str) -> str | None:
        return self._active_configuration_ids.get(_owner(session_id, thread_id))

    def set_active(self, session_id: str, configuration_id: str, thread_id: str) -> None:
        self.get(session_id, thread_id, configuration_id)
        self._active_configuration_ids[_owner(session_id, thread_id)] = configuration_id

    def clear_active(self, session_id: str, thread_id: str) -> None:
        self._active_configuration_ids.pop(_owner(session_id, thread_id), None)

    def active_name(self, session_id: str, thread_id: str) -> str | None:
        configuration_id = self.active_id(session_id, thread_id)
        return (
            self.get(session_id, thread_id, configuration_id).name
            if configuration_id is not None
            else None
        )

    def active_revision(self, session_id: str, thread_id: str) -> int:
        configuration_id = self.active_id(session_id, thread_id)
        return (
            self.get(session_id, thread_id, configuration_id).revision
            if configuration_id is not None
            else 0
        )

    def selection(self, session_id: str, thread_id: str) -> NodeDebugLaunchSelection:
        selection = self._launch_selections.get(_owner(session_id, thread_id))
        if selection is None:
            return NodeDebugLaunchSelection()
        return NodeDebugLaunchSelection(
            script_path=selection.script_path,
            working_directory=selection.working_directory,
            launch_profile_name=selection.launch_profile_name,
            args=list(selection.args),
        )

    def active_breakpoints(
        self, session_id: str, thread_id: str
    ) -> list[NodeDebugBreakpointDTO]:
        configuration_id = self.active_id(session_id, thread_id)
        if configuration_id is None:
            return []
        configuration = self.get(session_id, thread_id, configuration_id)
        return [
            persistable_breakpoint(runtime_breakpoint(breakpoint))
            for breakpoint in configuration.breakpoints
        ]

    def set_selection(
        self, owner: NodeDebugOwner, selection: NodeDebugLaunchSelection
    ) -> None:
        self._launch_selections[owner] = NodeDebugLaunchSelection(
            script_path=selection.script_path,
            working_directory=selection.working_directory,
            launch_profile_name=selection.launch_profile_name,
            args=list(selection.args),
        )

    def select_for_start(
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
        selected_id = configuration_id or self.active_id(session_id, thread_id)
        if selected_id is None:
            _, relative_path = self._configuration_factory.resolve_script_path(path)
            configuration = self._configuration_factory.configuration_from_request(
                configuration_id=create_prefixed_id("dbgcfg"),
                name=f"调试 {Path(relative_path).name}",
                script_path=relative_path,
                working_directory=working_directory or "",
                launch_profile_name=launch_profile_name,
                args=args,
                breakpoints=[],
            )
            self.put(session_id, configuration, thread_id)
            selected_id = configuration.configuration_id
        self.get(session_id, thread_id, selected_id)
        if self.active_id(session_id, thread_id) != selected_id:
            self.activate(session_id, selected_id, thread_id=thread_id)
        return selected_id

    def ensure_configuration_for_breakpoint(
        self, session_id: str, thread_id: str, *, path: str
    ) -> None:
        if self.active_id(session_id, thread_id) is not None:
            return
        _, relative_path = self._configuration_factory.resolve_script_path(path)
        configuration = self._configuration_factory.configuration_from_request(
            configuration_id=create_prefixed_id("dbgcfg"),
            name=f"调试 {Path(relative_path).name}",
            script_path=relative_path,
            working_directory="",
            launch_profile_name="node-default",
            args=[],
            breakpoints=[],
        )
        self.put(session_id, configuration, thread_id)
        self.activate(session_id, configuration.configuration_id, thread_id=thread_id)

    def summaries(
        self, session_id: str, thread_id: str
    ) -> list[NodeDebugConfigurationSummaryDTO]:
        return [
            NodeDebugConfigurationSummaryDTO(
                configuration_id=configuration.configuration_id,
                name=configuration.name,
                script_path=configuration.script_path,
                launch_profile_name=configuration.launch_profile_name,
                breakpoint_count=len(configuration.breakpoints),
                revision=configuration.revision,
                updated_at=configuration.updated_at,
            )
            for configuration in self.list(session_id, thread_id)
        ]

    def assert_unique_name(
        self,
        session_id: str,
        name: str,
        *,
        thread_id: str,
        exclude_configuration_id: str | None = None,
    ) -> None:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("调试方案名称不能为空")
        for configuration in self._configurations.get(
            _owner(session_id, thread_id), {}
        ).values():
            if configuration.configuration_id == exclude_configuration_id:
                continue
            if configuration.name.casefold() == normalized:
                raise ValueError(f"调试方案名称已存在: {name.strip()}")

    def _load_active_configuration(self, session_id: str, thread_id: str) -> None:
        owner = _owner(session_id, thread_id)
        configuration_id = self.active_id(session_id, thread_id)
        if configuration_id is None:
            raise RuntimeError(
                f"会话没有活动调试方案: session_id={session_id}, thread_id={thread_id}"
            )
        configuration = self.get(session_id, thread_id, configuration_id)
        self.set_selection(
            owner,
            NodeDebugLaunchSelection(
                script_path=configuration.script_path,
                working_directory=configuration.working_directory,
                launch_profile_name=configuration.launch_profile_name,
                args=list(configuration.args),
            ),
        )


__all__ = ["NodeDebugConfigurationRegistry", "NodeDebugLaunchSelection"]
