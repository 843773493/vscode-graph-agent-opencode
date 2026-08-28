from __future__ import annotations

from collections.abc import Callable

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationSummaryDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug_session_store import (
    NodeDebugSessionStore,
)


class NodeDebugConfigurationRegistry:
    """管理会话内调试方案发现、活动选择和持久化索引。"""

    def __init__(
        self,
        *,
        store: NodeDebugSessionStore | None,
        validate_configuration: Callable[
            [NodeDebugConfigurationDTO], NodeDebugConfigurationDTO
        ],
    ) -> None:
        self._store = store
        self._validate_configuration = validate_configuration
        self._configurations: dict[
            str, dict[str, NodeDebugConfigurationDTO]
        ] = {}
        self._active_configuration_ids: dict[str, str] = {}
        self._loaded_sessions: set[str] = set()

    def ensure_loaded(self, session_id: str) -> NodeDebugSessionManifestDTO | None:
        if session_id in self._loaded_sessions:
            return None
        configurations = (
            self._store.list_configurations(session_id)
            if self._store is not None
            else []
        )
        by_id: dict[str, NodeDebugConfigurationDTO] = {}
        names: set[str] = set()
        for configuration in configurations:
            validated = self._validate_configuration(configuration)
            if validated.configuration_id in by_id:
                raise RuntimeError(
                    f"会话存在重复调试方案 ID: {validated.configuration_id}"
                )
            normalized_name = validated.name.casefold()
            if normalized_name in names:
                raise RuntimeError(
                    f"会话存在重复调试方案名称: {validated.name}"
                )
            names.add(normalized_name)
            by_id[validated.configuration_id] = validated
        self._configurations[session_id] = by_id
        manifest = (
            self._store.read_manifest(session_id)
            if self._store is not None
            else None
        )
        if manifest is not None and manifest.active_configuration_id is not None:
            if manifest.active_configuration_id not in by_id:
                raise RuntimeError(
                    "活动调试方案不存在: "
                    f"session_id={session_id}, "
                    f"configuration_id={manifest.active_configuration_id}"
                )
            self._active_configuration_ids[session_id] = (
                manifest.active_configuration_id
            )
        self._loaded_sessions.add(session_id)
        return manifest

    def refresh_new_files(self, session_id: str) -> None:
        if self._store is None:
            return
        known = self._configurations.setdefault(session_id, {})
        for configuration in self._store.list_configurations(session_id):
            if configuration.configuration_id in known:
                continue
            validated = self._validate_configuration(configuration)
            self.assert_unique_name(session_id, validated.name)
            known[validated.configuration_id] = validated

    def list(self, session_id: str) -> list[NodeDebugConfigurationDTO]:
        return [
            configuration.model_copy(deep=True)
            for configuration in sorted(
                self._configurations.get(session_id, {}).values(),
                key=lambda item: (item.name.casefold(), item.configuration_id),
            )
        ]

    def get(
        self,
        session_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO:
        configuration = self._configurations.get(session_id, {}).get(
            configuration_id
        )
        if configuration is None:
            raise FileNotFoundError(f"调试方案不存在: {configuration_id}")
        return configuration

    def contains(self, session_id: str, configuration_id: str) -> bool:
        return configuration_id in self._configurations.get(session_id, {})

    def put(self, session_id: str, configuration: NodeDebugConfigurationDTO) -> None:
        self._configurations.setdefault(session_id, {})[
            configuration.configuration_id
        ] = configuration
        if self._store is not None:
            self._store.write_configuration(session_id, configuration)

    def remove(self, session_id: str, configuration_id: str) -> None:
        self.get(session_id, configuration_id)
        del self._configurations[session_id][configuration_id]
        if self._store is not None:
            self._store.delete_configuration(session_id, configuration_id)

    def active_id(self, session_id: str) -> str | None:
        return self._active_configuration_ids.get(session_id)

    def set_active(self, session_id: str, configuration_id: str) -> None:
        self.get(session_id, configuration_id)
        self._active_configuration_ids[session_id] = configuration_id

    def clear_active(self, session_id: str) -> None:
        self._active_configuration_ids.pop(session_id, None)

    def active_name(self, session_id: str) -> str | None:
        configuration_id = self.active_id(session_id)
        return (
            self.get(session_id, configuration_id).name
            if configuration_id is not None
            else None
        )

    def active_revision(self, session_id: str) -> int:
        configuration_id = self.active_id(session_id)
        return (
            self.get(session_id, configuration_id).revision
            if configuration_id is not None
            else 0
        )

    def summaries(
        self,
        session_id: str,
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
            for configuration in self.list(session_id)
        ]

    def assert_unique_name(
        self,
        session_id: str,
        name: str,
        *,
        exclude_configuration_id: str | None = None,
    ) -> None:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("调试方案名称不能为空")
        for configuration in self._configurations.get(session_id, {}).values():
            if configuration.configuration_id == exclude_configuration_id:
                continue
            if configuration.name.casefold() == normalized:
                raise ValueError(f"调试方案名称已存在: {name.strip()}")

    def write_manifest(
        self,
        manifest: NodeDebugSessionManifestDTO,
    ) -> None:
        if self._store is not None:
            self._store.write_manifest(manifest)


__all__ = ["NodeDebugConfigurationRegistry"]
