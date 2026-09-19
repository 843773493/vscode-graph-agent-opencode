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
from app.services.infrastructure.node_debug_thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
)


def _owner(session_id: str, thread_id: str) -> NodeDebugOwner:
    return normalize_node_debug_owner(session_id, thread_id)


class NodeDebugConfigurationRegistry:
    """管理会话内调试方案发现、活动选择和持久化索引。

    所有状态都以精确 ``(session_id, thread_id)`` owner key 索引；调用方必须显式
    提供 thread_id，本类不再提供隐式的 ``"main"`` 默认值。
    """

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
            NodeDebugOwner, dict[str, NodeDebugConfigurationDTO]
        ] = {}
        self._active_configuration_ids: dict[NodeDebugOwner, str] = {}
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
            self._active_configuration_ids[owner] = (
                manifest.active_configuration_id
            )
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
            validated = self._validate_configuration(configuration)
            self.assert_unique_name(session_id, validated.name, thread_id=thread_id)
            known[validated.configuration_id] = validated

    def list(
        self, session_id: str, thread_id: str
    ) -> list[NodeDebugConfigurationDTO]:
        return [
            configuration.model_copy(deep=True)
            for configuration in sorted(
                self._configurations.get(_owner(session_id, thread_id), {}).values(),
                key=lambda item: (item.name.casefold(), item.configuration_id),
            )
        ]

    def get(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
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

    def contains(
        self, session_id: str, configuration_id: str, thread_id: str
    ) -> bool:
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

    def remove(
        self, session_id: str, configuration_id: str, thread_id: str
    ) -> None:
        self.get(session_id, configuration_id, thread_id)
        del self._configurations[_owner(session_id, thread_id)][configuration_id]
        if self._store is not None:
            self._store.delete_configuration(session_id, configuration_id, thread_id)

    def active_id(self, session_id: str, thread_id: str) -> str | None:
        return self._active_configuration_ids.get(_owner(session_id, thread_id))

    def set_active(
        self, session_id: str, configuration_id: str, thread_id: str
    ) -> None:
        self.get(session_id, configuration_id, thread_id)
        self._active_configuration_ids[_owner(session_id, thread_id)] = configuration_id

    def clear_active(self, session_id: str, thread_id: str) -> None:
        self._active_configuration_ids.pop(_owner(session_id, thread_id), None)

    def active_name(self, session_id: str, thread_id: str) -> str | None:
        configuration_id = self.active_id(session_id, thread_id)
        return (
            self.get(session_id, configuration_id, thread_id).name
            if configuration_id is not None
            else None
        )

    def active_revision(self, session_id: str, thread_id: str) -> int:
        configuration_id = self.active_id(session_id, thread_id)
        return (
            self.get(session_id, configuration_id, thread_id).revision
            if configuration_id is not None
            else 0
        )

    def summaries(
        self,
        session_id: str,
        thread_id: str,
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

    def write_manifest(
        self,
        manifest: NodeDebugSessionManifestDTO,
    ) -> None:
        if self._store is not None:
            self._store.write_manifest(manifest)


__all__ = ["NodeDebugConfigurationRegistry"]
