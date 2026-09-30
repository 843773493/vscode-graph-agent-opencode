"""工作区配置服务（facade）：组合各方法族 mixin，对外保持 ConfigService 单一出口。

本包只保留 ConfigService 的 __init__、极少量直通访问器，以及模块级 inline VRN
判定与来源层权威表（后者唯一定义在 config_service_common）；其余 100+ 方法按族
逐字搬迁到同包各 mixin，由 ConfigService 多继承装配（共享同一 self 与 __init__
槽位）。对外导入路径与符号名保持不变：
``app.services.infrastructure.config_service.ConfigService``。
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.core.path_utils import (
    get_user_workspace_config_path,
)
from app.schemas.internal_v2.config import ConfigDTO
from app.services.infrastructure.config import (
    ConfigFileWatcher,
    ConfigSnapshot,
    ConfigSnapshotStore,
    WorkspaceSourceOwner,
)
from app.services.infrastructure.config.event_cursor import ConfigEventCursor
from app.services.infrastructure.config.llm_resolution import LlmResolution
from app.services.infrastructure.config.pending_restart import (
    PendingRestartCoordinator,
)
from app.services.infrastructure.config.reload_status import (
    ReloadStatusCoordinator,
)
from app.services.infrastructure.config.session_defaults import SessionDefaults
from app.services.infrastructure.config.shadow_adapter import (
    ConfigShadowLifecycleAdapter,
)
from app.services.infrastructure.config.state import (
    new_config_id,
)
from app.services.infrastructure.config_service.config_agent_tools import (
    ConfigAgentToolsMixin,
)
from app.services.infrastructure.config_service.config_agents import ConfigAgentsMixin
from app.services.infrastructure.config_service.config_public import ConfigPublicMixin
from app.services.infrastructure.config_service.config_reload import ConfigReloadMixin
from app.services.infrastructure.config_service.config_runtime_accessors import (
    ConfigRuntimeAccessorsMixin,
)
from app.services.infrastructure.config_service.config_service_common import (
    ConfigCandidateApplier,
    release_inline_config_vrn_for_file,
)
from app.services.infrastructure.config_service.config_snapshot import (
    ConfigSnapshotMixin,
)
from app.services.infrastructure.config_service.config_source_layers import (
    ConfigSourceLayersMixin,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import resolve_config_resource_source


class ConfigService(ConfigSourceLayersMixin,
    ConfigSnapshotMixin,
    ConfigRuntimeAccessorsMixin,
    ConfigReloadMixin,
    ConfigPublicMixin,
    ConfigAgentsMixin,
    ConfigAgentToolsMixin,
):
    _RUNTIME_OVERRIDE_CONFIG_KEY = "workspace_runtime_override"
    _CONFIG_DOMAIN = "workspace"
    _CANDIDATE_REF_ENV = "BOXTEAM_CONFIG_CANDIDATE_REF"
    _GENERATION_ENV = "BOXTEAM_CONFIG_GENERATION"
    _FENCING_TOKEN_ENV = "BOXTEAM_CONFIG_FENCING_TOKEN"

    def __init__(
        self,
        *,
        config_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        workspace_root: str | Path | None = None,
        inline_config_path: str | Path | None = None,
        workspace_state_store: WorkspaceStateStore | None = None,
        source_owner: WorkspaceSourceOwner | None = None,
        source_owner_workspace_id: str | None = None,
        event_channel_service: EventChannelService | None = None,
    ) -> None:
        resolved_config_dir = (
            Path(config_dir).expanduser().resolve()
            if config_dir
            else get_user_workspace_config_path().parent
        )
        self._config_dir = resolved_config_dir
        self._config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )
        self._inline_config_path = (
            Path(inline_config_path).expanduser().resolve()
            if inline_config_path
            else resolve_config_resource_source("workspace_inline.jsonc")
        )
        # inline 来源的逻辑资源名（VRN 尾段，去扩展名；如 workspace_inline）。
        self._inline_logical_name = self._inline_config_path.stem or "workspace_inline"
        self._schema: dict[str, Any] | None = None
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve() if workspace_root else None
        )
        self._workspace_state_store = workspace_state_store
        if (source_owner is None) != (source_owner_workspace_id is None):
            raise ValueError(
                "Workspace source owner 必须同时提供 owner 和 workspace_id"
            )
        self._source_owner = source_owner
        self._source_owner_workspace_id = source_owner_workspace_id
        if self._workspace_state_store is not None:
            self._workspace_state_store.recover_expired_config_applies(
                config_domain=self._CONFIG_DOMAIN
            )
        runtime_override = (
            workspace_state_store.get_source_layer(self._RUNTIME_OVERRIDE_CONFIG_KEY)
            if workspace_state_store is not None
            else None
        )
        self._runtime_config_overrides: dict[str, Any] = (
            dict(runtime_override.payload)
            if runtime_override is not None and runtime_override.payload is not None
            else {}
        )
        self._mcp_tool_names: frozenset[str] | None = None
        self._snapshot_store = ConfigSnapshotStore(
            candidate_builder=self._build_candidate_snapshot,
        )
        self._pinned_snapshot: ContextVar[ConfigSnapshot | None] = ContextVar(
            f"config_snapshot_{id(self)}",
            default=None,
        )
        self._watcher: ConfigFileWatcher | None = None
        self._candidate_applier: ConfigCandidateApplier | None = None
        self._shadow_lifecycle = ConfigShadowLifecycleAdapter(
            validator=self._validate_shadow_config,
            reconcile=self._reconcile_shadow_config,
            event_service=event_channel_service,
            bootstrap_guard_keys=("config_version",),
            domain=self._CONFIG_DOMAIN,
        )
        self._loaded_source: str | None = None
        self._runtime_generation = (
            os.environ.get("BOXTEAM_CONFIG_GENERATION", "").strip()
            or new_config_id("workspace_generation")
        )
        self._reload_status = ReloadStatusCoordinator(
            store=self._workspace_state_store,
            config_domain=self._CONFIG_DOMAIN,
            snapshot_status_provider=self._snapshot_store.status,
        )
        self._pending_restart = PendingRestartCoordinator(
            store=self._workspace_state_store,
            config_domain=self._CONFIG_DOMAIN,
            reload_status_provider=self._reload_status.get_reload_status,
        )
        self._event_cursor = ConfigEventCursor(
            store=self._workspace_state_store,
            config_domain=self._CONFIG_DOMAIN,
        )
        self._llm_resolution = LlmResolution(
            effective_config_provider=self._get_effective_config,
            default_agent_id_provider=self.get_default_agent_id,
        )
        self._session_defaults = SessionDefaults(
            workspace_root=self._workspace_root,
            validate_agent_id=self.validate_agent_id,
            resolve_agent_provider_id=self.resolve_agent_provider_id,
            default_agent_id_provider=self.get_default_agent_id,
        )

    def _get_effective_config(self) -> dict[str, Any]:
        return self._require_snapshot().to_dict()

    async def get(self) -> ConfigDTO:
        return self._build_public_config()


__all__ = ["ConfigService", "release_inline_config_vrn_for_file"]
