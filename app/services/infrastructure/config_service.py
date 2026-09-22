from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import jsonschema

from app.agents.custom_tools import load_custom_tool_factory
from app.agents.policy import (
    ResolvedToolPolicy,
    ToolPolicyResolver,
    build_agent_tool_universe,
    custom_tool_spec_names,
    parse_custom_tool_specs,
    resolve_tool_policy,
    resolve_tool_selectors,
)
from app.core.config_sources import (
    ConfigSource,
    config_revision,
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import (
    get_user_workspace_config_path,
    get_user_workspace_local_config_path,
    get_user_workspace_schema_path,
    get_workspace_config_path,
)
from app.schemas.internal_v2.config import ConfigDTO, ConfigUpdateRequest
from app.services.infrastructure.config import (
    SHARED_USER_WORKSPACE_SOURCE_KEY,
    ConfigFileWatcher,
    ConfigReloadStatus,
    ConfigRestartRequiredError,
    ConfigSnapshot,
    ConfigSnapshotStore,
    WorkspaceSourceOwner,
    build_config_snapshot,
)
from app.services.infrastructure.config.event_cursor import ConfigEventCursor
from app.services.infrastructure.config.llm_resolution import LlmResolution
from app.services.infrastructure.config.pending_restart import (
    PendingRestartCoordinator,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.reload_status import (
    ReloadStatusCoordinator,
)
from app.services.infrastructure.config.session_defaults import SessionDefaults
from app.services.infrastructure.config.shadow_adapter import (
    ConfigShadowLifecycleAdapter,
)
from app.services.infrastructure.config.shadow_scope import ConfigShadowLifecycleError
from app.services.infrastructure.config.state import (
    ConfigActiveSnapshotRecord,
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    ConfigResult,
    SecretReferenceRequiredError,
    build_secret_binding_summary,
    changed_json_paths,
    dump_json,
    new_config_id,
    prepare_config_for_persistence,
    redact_config_payload,
    restore_environment_secret_references,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import resolve_config_resource_source
from configs.layout_migrations import migrate_legacy_workspace_configuration
from configs.runtime import merge_json_objects, read_jsonc_object

logger = logging.getLogger(__name__)

ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]


class ConfigService:
    _RUNTIME_OVERRIDE_CONFIG_KEY = "workspace_runtime_override"
    _CONFIG_DOMAIN = "workspace"

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
            workspace_state_store.get_config(self._RUNTIME_OVERRIDE_CONFIG_KEY)
            if workspace_state_store is not None
            else None
        )
        self._runtime_config_overrides: dict[str, Any] = (
            dict(runtime_override.payload) if runtime_override is not None else {}
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

    _CANDIDATE_REF_ENV = "BOXTEAM_CONFIG_CANDIDATE_REF"
    _GENERATION_ENV = "BOXTEAM_CONFIG_GENERATION"
    _FENCING_TOKEN_ENV = "BOXTEAM_CONFIG_FENCING_TOKEN"

    def _resolve_schema_path(self) -> Path:
        config_path = self._get_workspace_config_path()
        if self._workspace_state_store is None and config_path.is_file():
            schema_reference = read_jsonc_object(config_path).get("$schema")
            if (
                isinstance(schema_reference, str)
                and schema_reference
                and "://" not in schema_reference
            ):
                referenced_path = (config_path.parent / schema_reference).resolve()
                if referenced_path.is_file():
                    return referenced_path
        if self._inline_config_path.is_file():
            schema_reference = read_jsonc_object(
                self._inline_config_path
            ).get("$schema")
            if (
                isinstance(schema_reference, str)
                and schema_reference
                and "://" not in schema_reference
            ):
                referenced_path = (
                    self._inline_config_path.parent / schema_reference
                ).resolve()
                if referenced_path.is_file():
                    return referenced_path
        configured_schema = self._config_dir / "workspace_schema.jsonc"
        if configured_schema.is_file():
            return configured_schema
        installed_schema = get_user_workspace_schema_path()
        if installed_schema.is_file():
            return installed_schema
        raise FileNotFoundError(
            "Workspace 配置 schema 不存在: "
            f"config={config_path} expected={configured_schema}"
        )

    def _load_schema(self) -> dict[str, Any]:
        if self._schema is None:
            self._schema = read_jsonc_object(self._resolve_schema_path())
        return self._schema

    def _get_workspace_config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        return get_user_workspace_config_path()

    def _read_effective_config(
        self,
    ) -> tuple[
        dict[str, Any],
        tuple[Path, ...],
        tuple[ConfigSource, ...],
    ]:
        config_path = self._get_workspace_config_path()
        source_paths: list[Path] = [self._inline_config_path]
        source_details: list[ConfigSource] = [
            ConfigSource(
                path=self._inline_config_path,
                layer="inline",
                precedence=0,
                loaded=True,
            ),
        ]
        config = self._read_config_source(self._inline_config_path)
        user_override = self._read_shared_override(
            config_key="workspace_mutable_override",
            path=config_path,
        )
        source_details.append(
            self._config_source(
                path=config_path,
                layer="user",
                precedence=1,
                config_key="workspace_mutable_override",
            )
        )
        if user_override is not None:
            config = merge_json_objects(
                config,
                user_override,
            )
            self._append_source_path(source_paths, config_path)

        local_config_path = self._get_workspace_local_config_path()
        local_override = self._read_shared_override(
            config_key="workspace_local_mutable_override",
            path=local_config_path,
        )
        source_details.append(
            self._config_source(
                path=local_config_path,
                layer="user_local",
                precedence=2,
                config_key="workspace_local_mutable_override",
            )
        )
        if local_override is not None:
            config = merge_json_objects(
                config,
                local_override,
            )
            self._append_source_path(source_paths, local_config_path)

        if self._workspace_root is not None:
            migrate_legacy_workspace_configuration(
                workspace_root=self._workspace_root,
                workspace_schema_path=self._resolve_schema_path(),
            )
            workspace_path = get_workspace_config_path(self._workspace_root)
            workspace_override = self._read_shared_override(
                config_key="workspace_root_mutable_override",
                path=workspace_path,
            )
            source_details.append(
                self._config_source(
                    path=workspace_path,
                    layer="workspace",
                    precedence=3,
                    config_key="workspace_root_mutable_override",
                )
            )
            if workspace_override is not None:
                config = merge_json_objects(config, workspace_override)
                self._append_source_path(source_paths, workspace_path)
        if self._workspace_state_store is not None or self._runtime_config_overrides:
            source_details.append(self._runtime_override_source())
        if self._runtime_config_overrides:
            config["ui"] = merge_json_objects(
                config.get("ui", {})
                if isinstance(config.get("ui", {}), dict)
                else {},
                self._runtime_config_overrides,
            )
        self._validate_agent_tool_policies(config)
        return config, tuple(source_paths), tuple(source_details)

    def _runtime_override_source(self) -> ConfigSource:
        if self._workspace_state_store is None:
            return ConfigSource(
                path=self._config_dir / "workspace_runtime_override",
                layer="sqlite",
                precedence=4,
                loaded=bool(self._runtime_config_overrides),
                source_key=self._RUNTIME_OVERRIDE_CONFIG_KEY,
                presence=("present" if self._runtime_config_overrides else "absent"),
            )
        source_record = self._workspace_state_store.get_source_layer(
            self._RUNTIME_OVERRIDE_CONFIG_KEY
        )
        legacy_record = self._workspace_state_store.get_config(
            self._RUNTIME_OVERRIDE_CONFIG_KEY
        )
        return ConfigSource(
            path=self._workspace_state_store.path,
            layer="sqlite",
            precedence=4,
            loaded=bool(self._runtime_config_overrides),
            source_key=self._RUNTIME_OVERRIDE_CONFIG_KEY,
            presence=(
                source_record.presence
                if source_record is not None
                else ("present" if legacy_record is not None else "absent")
            ),
            layer_revision=(
                source_record.layer_revision if source_record is not None else None
            ),
            layer_digest=(
                source_record.layer_digest if source_record is not None else None
            ),
            source_generation=(
                source_record.source_generation if source_record is not None else None
            ),
        )

    def _sync_runtime_override_source(
        self,
        *,
        updates: dict[str, Any] | None = None,
        base_layer_revision: int | None = None,
        base_layer_digest: str | None = None,
        expected_active_revision: int | None = None,
        expected_active_digest: str | None = None,
    ) -> None:
        if self._workspace_state_store is None:
            if updates is not None:
                for key, value in updates.items():
                    if value is None:
                        self._runtime_config_overrides.pop(key, None)
                    else:
                        self._runtime_config_overrides[key] = value
            return
        source_key = self._RUNTIME_OVERRIDE_CONFIG_KEY
        current = self._workspace_state_store.get_source_layer(source_key)
        next_overrides = dict(
            current.payload
            if current is not None and current.payload is not None
            else self._runtime_config_overrides
        )
        if updates is not None:
            for key, value in updates.items():
                if value is None:
                    next_overrides.pop(key, None)
                else:
                    next_overrides[key] = value
        payload = next_overrides or None
        record = self._workspace_state_store.sync_config_source(
            config_key=source_key,
            source_path=self._workspace_state_store.path,
            config_version=1,
            presence="present" if payload is not None else "absent",
            payload=payload,
            layer_digest=config_revision(payload) if payload is not None else None,
            expected_layer_revision=base_layer_revision,
            expected_layer_digest=base_layer_digest,
            journal_origin="api",
            config_domain=self._CONFIG_DOMAIN,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
            enforce_layer_cas=True,
            enforce_active_cas=True,
        )
        self._runtime_config_overrides = dict(record.payload or {})
        self._workspace_state_store.append_config_source_journal(
            source_key=source_key,
            source_event_id=f"{source_key}:layer:{record.layer_revision}",
            source_path=Path(record.source_path),
            presence=record.presence,
            layer_revision=record.layer_revision,
            layer_digest=record.layer_digest,
            previous_digest=record.previous_digest,
            origin="api",
            fanout_id=f"fanout:{source_key}:event:{source_key}:layer:{record.layer_revision}",
            expected_source_generation=self._workspace_state_store.source_generation_high_water_mark(
                source_key=source_key
            ),
        )

    def _config_source(
        self,
        *,
        path: Path,
        layer: str,
        precedence: int,
        config_key: str,
    ) -> ConfigSource:
        if self._workspace_state_store is None:
            return ConfigSource(
                path=path,
                layer=layer,  # type: ignore[arg-type]
                precedence=precedence,
                loaded=path.is_file(),
                source_key=config_key,
                presence="present" if path.is_file() else "absent",
            )
        source_record = self._workspace_state_store.get_source_layer(config_key)
        legacy_record = self._workspace_state_store.get_config(config_key)
        return ConfigSource(
            path=self._workspace_state_store.path,
            layer="sqlite",
            precedence=precedence,
            loaded=(
                source_record.presence == "present"
                if source_record is not None
                else legacy_record is not None or path.is_file()
            ),
            presence=(
                source_record.presence
                if source_record is not None
                else ("present" if legacy_record is not None or path.is_file() else "absent")
            ),
            source_key=config_key,
            layer_revision=(
                source_record.layer_revision if source_record is not None else None
            ),
            layer_digest=(
                source_record.layer_digest if source_record is not None else None
            ),
            source_generation=(
                source_record.source_generation if source_record is not None else None
            ),
        )

    def _append_source_path(self, source_paths: list[Path], path: Path) -> None:
        resolved_path = (
            self._workspace_state_store.path
            if self._workspace_state_store is not None
            else path
        )
        if resolved_path not in source_paths:
            source_paths.append(resolved_path)

    def _has_shared_override(self, *, config_key: str, path: Path) -> bool:
        if self._workspace_state_store is None:
            return path.is_file()
        source_record = self._workspace_state_store.get_source_layer(config_key)
        if source_record is not None:
            return source_record.presence == "present"
        return self._workspace_state_store.get_config(config_key) is not None or path.is_file()

    def _read_shared_override(
        self,
        *,
        config_key: str,
        path: Path,
    ) -> dict[str, Any] | None:
        if self._workspace_state_store is None:
            return self._read_config_source(path) if path.is_file() else None
        blocked = self._workspace_state_store.migrate_legacy_config_secrets(config_key)
        if blocked:
            raise SecretReferenceRequiredError(
                "旧 Workspace SQLite 含无法恢复的秘密摘要，必须重新导入: "
                + ", ".join(blocked)
            )
        record = self._workspace_state_store.get_config(config_key)
        source_record = self._workspace_state_store.get_source_layer(config_key)
        previous_source_generation = (
            source_record.source_generation if source_record is not None else 0
        )
        file_snapshot = read_stable_config_file(path)
        journal_origin = (
            None
            if self._is_shared_user_source(config_key=config_key, path=path)
            else "file-watcher"
        )
        if file_snapshot.presence == "absent":
            if source_record is None and record is None:
                return None
            deleted_backup_path = path.with_name(f"{path.name}.deleted.bak")
            if (
                not deleted_backup_path.exists()
                and record is not None
            ):
                deleted_backup_path.write_text(
                    dump_json(redact_config_payload(record.payload)),
                    encoding="utf-8",
                )
            verify_stable_config_file(file_snapshot)
            source_record = self._workspace_state_store.sync_config_source(
                config_key=config_key,
                source_path=path,
                config_version=(
                    source_record.config_version
                    if source_record is not None
                    else record.config_version
                ),
                presence="absent",
                payload=None,
                layer_digest=None,
                expected_layer_revision=(
                    source_record.layer_revision if source_record is not None else None
                ),
                expected_layer_digest=(
                    source_record.layer_digest if source_record is not None else None
                ),
                backup_path=deleted_backup_path if record is not None else None,
                journal_origin=journal_origin,
            )
            self._record_source_journal(
                source_record,
                origin="file-watcher",
                previous_source_generation=previous_source_generation,
            )
            return None

        if file_snapshot.digest is None:
            raise RuntimeError(f"present 配置文件缺少 digest: {path}")
        if (
            source_record is not None
            and source_record.presence == "present"
            and source_record.layer_digest == file_snapshot.digest
            and source_record.source_path == str(path.expanduser().resolve())
        ):
            payload = parse_stable_config_file(file_snapshot)
            if payload is None:
                raise RuntimeError(f"present 配置文件解析为空: {path}")
            prepare_config_for_persistence(payload)
            self._preflight_override(payload, source_path=self._workspace_state_store.path)
            verify_stable_config_file(file_snapshot)
            source_record = self._workspace_state_store.sync_config_source(
                config_key=config_key,
                source_path=path,
                config_version=int(payload.get("config_version", 1)),
                presence="present",
                payload=payload,
                layer_digest=file_snapshot.digest,
                expected_layer_revision=source_record.layer_revision,
                expected_layer_digest=source_record.layer_digest,
                journal_origin=("loader" if journal_origin is not None else None),
            )
            self._record_source_journal(
                source_record,
                origin="loader",
                previous_source_generation=previous_source_generation,
            )
            return dict(payload)

        try:
            payload = parse_stable_config_file(file_snapshot)
        except Exception:
            if record is not None and not self._snapshot_store.has_snapshot():
                restored = restore_environment_secret_references(record.payload)
                if not isinstance(restored, dict):
                    raise TypeError("SQLite 兼容配置恢复结果必须是对象")
                prepare_config_for_persistence(restored)
                payload = restored
            else:
                raise
        if payload is None:
            raise RuntimeError(f"present 配置文件解析为空: {path}")
        prepare_config_for_persistence(payload)
        self._preflight_override(payload, source_path=path)
        verify_stable_config_file(file_snapshot)
        backup_path = path.with_name(f"{path.name}.migrated.bak")
        if not backup_path.exists():
            shutil.copy2(path, backup_path)
        source_record = self._workspace_state_store.sync_config_source(
            config_key=config_key,
            source_path=path,
            config_version=int(payload.get("config_version", 1)),
            presence="present",
            payload=payload,
            layer_digest=file_snapshot.digest,
            expected_layer_revision=(
                source_record.layer_revision if source_record is not None else None
            ),
            expected_layer_digest=(
                source_record.layer_digest if source_record is not None else None
            ),
            backup_path=backup_path,
            journal_origin=journal_origin,
        )
        self._record_source_journal(
            source_record,
            origin="file-watcher",
            previous_source_generation=previous_source_generation,
        )
        return payload

    def _record_source_journal(
        self,
        source_record,
        *,
        origin: str,
        previous_source_generation: int = 0,
    ) -> None:
        if self._workspace_state_store is None:
            return
        source_key = source_record.config_key
        if self._is_shared_user_source(
            config_key=source_key,
            path=Path(source_record.source_path),
        ):
            source_owner = self._source_owner
            workspace_id = self._source_owner_workspace_id
            if source_owner is None or workspace_id is None:
                raise RuntimeError("共享 Workspace source 缺少 source owner 身份")
            owner_record = source_owner.observe(
                source_path=Path(source_record.source_path),
                presence=source_record.presence,
                layer_digest=source_record.layer_digest,
                origin=origin,
                source_key=SHARED_USER_WORKSPACE_SOURCE_KEY,
            )
            materialized = self._workspace_state_store.update_source_generation(
                config_key=source_key,
                source_generation=owner_record.source_generation,
                expected_layer_revision=source_record.layer_revision,
                expected_layer_digest=source_record.layer_digest,
            )
            source_owner.prepare_fanout(
                workspace_id=workspace_id,
                source_key=SHARED_USER_WORKSPACE_SOURCE_KEY,
                after_generation=previous_source_generation,
            )
            for prior in source_owner.list_journal(
                source_key=SHARED_USER_WORKSPACE_SOURCE_KEY,
                after_generation=previous_source_generation,
            ):
                is_current = prior.source_generation == owner_record.source_generation
                source_owner.record_fanout(
                    source_key=SHARED_USER_WORKSPACE_SOURCE_KEY,
                    source_generation=prior.source_generation,
                    workspace_id=workspace_id,
                    status="applied" if is_current else "superseded",
                    layer_revision=(materialized.layer_revision if is_current else None),
                    layer_digest=(materialized.layer_digest if is_current else None),
                    result="materialized" if is_current else "superseded",
                )
            if owner_record.source_generation <= previous_source_generation:
                source_owner.record_fanout(
                    source_key=SHARED_USER_WORKSPACE_SOURCE_KEY,
                    source_generation=owner_record.source_generation,
                    workspace_id=workspace_id,
                    status="applied",
                    layer_revision=materialized.layer_revision,
                    layer_digest=materialized.layer_digest,
                    result="materialized",
                )
            return
        self._workspace_state_store.append_config_source_journal(
            source_key=source_key,
            source_event_id=f"{source_key}:layer:{source_record.layer_revision}",
            source_path=Path(source_record.source_path),
            presence=source_record.presence,
            layer_revision=source_record.layer_revision,
            layer_digest=source_record.layer_digest,
            previous_digest=source_record.previous_digest,
            origin=origin,
            fanout_id=(
                f"fanout:{source_key}:event:{source_key}:"
                f"layer:{source_record.layer_revision}"
            ),
            expected_source_generation=(
                self._workspace_state_store.source_generation_high_water_mark(
                    source_key=source_key
                )
            ),
        )

    @staticmethod
    def _preflight_override(payload: dict[str, Any], *, source_path: Path) -> None:
        # SQLite 中的数据已经经过 JSON 解析；仍执行自定义工具工厂预检，保持来源边界一致。
        ConfigService._preflight_custom_tool_factories(payload, source_path=source_path)

    @staticmethod
    def _read_config_source(
        path: Path,
    ) -> dict[str, Any]:
        config_snapshot = read_stable_config_file(path)
        config = parse_stable_config_file(config_snapshot)
        if config is None:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        ConfigService._preflight_custom_tool_factories(config, source_path=path)
        return config

    def _apply_workspace_override(
        self,
        base_config: dict[str, Any],
        workspace_root: Path,
    ) -> tuple[
        dict[str, Any],
        Path | None,
    ]:
        override_path = get_workspace_config_path(workspace_root)
        if override_path.is_file():
            override_config = self._read_config_source(override_path)
            return (
                merge_json_objects(base_config, override_config),
                override_path,
            )
        return base_config, None

    def _is_shared_user_source(self, *, config_key: str, path: Path) -> bool:
        return (
            self._source_owner is not None
            and self._source_owner_workspace_id is not None
            and config_key == "workspace_mutable_override"
            and path.expanduser().resolve() == self._get_workspace_config_path().resolve()
        )

    def _get_workspace_local_config_path(self) -> Path:
        if self._config_path is None:
            return get_user_workspace_local_config_path()
        return self._get_workspace_config_path().parent / "workspace_local.jsonc"

    def _build_candidate_snapshot(
        self,
        *,
        validate_schema: bool = True,
    ) -> ConfigSnapshot:
        config, source_paths, source_details = self._read_effective_config()
        schema_path = self._resolve_schema_path()
        if validate_schema:
            jsonschema.validate(config, self._load_schema())
        snapshot = build_config_snapshot(
            config,
            source_paths=source_paths,
            source_details=source_details,
            schema_path=schema_path,
        )
        return snapshot

    def validate_workspace_config(self) -> None:
        snapshot = self._require_snapshot()
        jsonschema.validate(snapshot.to_dict(), self._load_schema())
        self._validate_agent_tool_policies(snapshot.to_dict())
        if self._workspace_state_store is None:
            return
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        if active is None and self._loaded_source == "source":
            self._persist_initial_active_snapshot()

    def _require_snapshot(self) -> ConfigSnapshot:
        pinned = self._pinned_snapshot.get()
        if pinned is not None:
            return pinned
        if not self._snapshot_store.has_snapshot():
            candidate_ref = os.environ.get(self._CANDIDATE_REF_ENV, "").strip()
            if candidate_ref:
                self._snapshot_store.initialize(
                    self._build_startup_pending_snapshot(candidate_ref),
                )
            elif self._workspace_state_store is not None:
                blocked = self._workspace_state_store.migrate_legacy_active_snapshot_secrets(
                    config_domain=self._CONFIG_DOMAIN
                )
                if blocked:
                    raise SecretReferenceRequiredError(
                        "旧 active snapshot 含无法恢复的秘密摘要，必须重新导入: "
                        + ", ".join(blocked)
                    )
                active = self._workspace_state_store.get_active_config_snapshot(
                    self._CONFIG_DOMAIN
                )
                if active is not None:
                    if active.state != "active":
                        raise ConfigConflictError(
                            "active snapshot 当前需要恢复，禁止从损坏记录启动: "
                            f"state={active.state}, error={active.last_error}"
                        )
                    self._snapshot_store.initialize(
                        self._build_persisted_snapshot(active, loaded_source="active")
                    )
                else:
                    # 首次启动没有 active snapshot 时才从 JSONC/source layers
                    # 建立初始快照；后续启动不得因为 source 已有 pending 就越过 active。
                    self._loaded_source = "source"
                    self._snapshot_store.initialize(
                        self._build_candidate_snapshot(validate_schema=False),
                    )
            else:
                self._loaded_source = "source"
                self._snapshot_store.initialize(
                    self._build_candidate_snapshot(validate_schema=False),
                )
        return self._snapshot_store.current()

    def _build_startup_pending_snapshot(self, candidate_ref: str) -> ConfigSnapshot:
        if self._workspace_state_store is None:
            raise ConfigConflictError(
                "Workspace candidate_ref 只能由 Workspace-owned SQLite loader 处理"
            )
        pending = self._workspace_state_store.load_pending_config_candidate(
            candidate_ref=candidate_ref
        )
        generation = os.environ.get(self._GENERATION_ENV, "").strip()
        fencing_token = os.environ.get(self._FENCING_TOKEN_ENV, "").strip()
        if not generation or not fencing_token:
            raise ConfigConflictError(
                "Workspace pending 启动缺少 generation 或 fencing token"
            )
        if pending.target_generation != generation:
            raise ConfigConflictError(
                "Workspace pending candidate 的 target generation 不匹配: "
                f"expected={pending.target_generation}, actual={generation}"
            )
        if pending.fencing_token != fencing_token:
            raise ConfigConflictError("Workspace pending candidate 的 fencing token 不匹配")
        return self._build_persisted_snapshot(pending, loaded_source="pending")

    def _build_persisted_snapshot(
        self,
        record: object,
        *,
        loaded_source: str,
    ) -> ConfigSnapshot:
        if not isinstance(record, (ConfigActiveSnapshotRecord, ConfigPendingCandidateRecord)):
            raise TypeError("持久化配置记录类型无效")
        prepare_config_for_persistence(
            record.payload,
            # 普通 active 恢复先保持旧运行时；只有用户确认的 pending
            # generation 才必须在启动证明阶段解析全部 secret reference。
            resolve_environment=loaded_source == "pending",
        )
        restored = restore_environment_secret_references(record.payload)
        if not isinstance(restored, dict):
            raise TypeError("持久化配置恢复结果必须是对象")
        jsonschema.validate(restored, self._load_schema())
        self._validate_agent_tool_policies(restored)
        source_details, source_paths = self._persisted_source_details(
            record.source_baseline
        )
        snapshot = build_config_snapshot(
            restored,
            source_paths=source_paths,
            source_details=source_details,
            schema_path=self._resolve_schema_path(),
        )
        expected_digest = record.effective_digest
        if snapshot.revision != expected_digest:
            raise ConfigConflictError(
                "持久化配置 digest 校验失败: "
                f"source={loaded_source}, expected={expected_digest}, actual={snapshot.revision}"
            )
        if (
            isinstance(record, ConfigPendingCandidateRecord)
            and record.candidate_digest != snapshot.revision
        ):
            raise ConfigConflictError("pending candidate digest 与 payload 不匹配")
        self._loaded_source = loaded_source
        return snapshot

    @staticmethod
    def _persisted_source_details(
        baseline: dict[str, object],
    ) -> tuple[tuple[ConfigSource, ...], tuple[Path, ...]]:
        layer_names = {
            "workspace_mutable_override": ("user", 1),
            "workspace_local_mutable_override": ("user_local", 2),
            "workspace_root_mutable_override": ("workspace", 3),
            "workspace_runtime_override": ("sqlite", 4),
        }
        details: list[ConfigSource] = []
        paths: list[Path] = []
        for fallback_precedence, (source_key, raw_value) in enumerate(
            sorted(baseline.items()),
            start=1,
        ):
            if not isinstance(raw_value, dict):
                raise TypeError(f"source baseline 必须是对象: key={source_key}")
            raw_path = raw_value.get("path")
            raw_presence = raw_value.get("presence")
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"source baseline 缺少 path: key={source_key}")
            if raw_presence not in {"present", "absent"}:
                raise ValueError(f"source baseline presence 无效: key={source_key}")
            layer, precedence = layer_names.get(
                source_key,
                ("sqlite", fallback_precedence),
            )
            source_path = Path(raw_path)
            details.append(
                ConfigSource(
                    path=source_path,
                    layer=layer,
                    precedence=precedence,
                    loaded=raw_presence == "present",
                    source_key=source_key,
                    presence=raw_presence,
                    layer_revision=(
                        int(raw_value["layer_revision"])
                        if raw_value.get("layer_revision") is not None
                        else None
                    ),
                    layer_digest=(
                        str(raw_value["layer_digest"])
                        if raw_value.get("layer_digest") is not None
                        else None
                    ),
                    source_generation=(
                        int(raw_value["source_generation"])
                        if raw_value.get("source_generation") is not None
                        else None
                    ),
                )
            )
            if source_path not in paths:
                paths.append(source_path)
        return tuple(details), tuple(paths)

    def get_loaded_config_proof(self) -> dict[str, object]:
        """返回可供 Gateway 校验的加载证明，不包含秘密或候选完整 payload。"""

        snapshot = self._require_snapshot()
        active = (
            self._workspace_state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
            if self._workspace_state_store is not None
            else None
        )
        pending = (
            self._workspace_state_store.get_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN
            )
            if self._workspace_state_store is not None
            else None
        )
        loaded_pending = (
            pending
            if self._loaded_source == "pending"
            and pending is not None
            and pending.candidate_ref
            else None
        )
        secret_bindings = build_secret_binding_summary(
            snapshot.to_dict(),
            resolve_environment=True,
        )
        secret_binding_digest = hashlib.sha256(
            json.dumps(
                secret_bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fencing_token = (
            loaded_pending.fencing_token if loaded_pending is not None else None
        )
        return {
            "config_domain": self._CONFIG_DOMAIN,
            "loaded_source": "pending" if loaded_pending is not None else "active",
            "candidate_id": (
                loaded_pending.candidate_id if loaded_pending is not None else None
            ),
            "loaded_commit_revision": (
                loaded_pending.pending_revision
                if loaded_pending is not None
                else active.active_revision if active is not None else None
            ),
            "effective_digest": snapshot.revision,
            "candidate_digest": (
                loaded_pending.candidate_digest if loaded_pending is not None else None
            ),
            "secret_binding_digest": secret_binding_digest,
            "generation_id": self._runtime_generation,
            "fencing_token_digest": (
                hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()
                if fencing_token
                else None
            ),
        }

    def get_pending_startup_contract(
        self,
        *,
        candidate_ref: str,
    ) -> dict[str, object]:
        """只返回新 Workspace generation 所需的 pending 绑定元数据。"""

        return self._pending_restart.get_pending_startup_contract(
            candidate_ref=candidate_ref
        )

    def record_pending_restart_failure(
        self,
        *,
        candidate_ref: str,
        error: str,
        old_runtime_recovered: bool = True,
    ) -> ConfigReloadStatus:
        """记录重启失败，并根据旧 generation 是否恢复选择恢复状态。"""

        return self._pending_restart.record_pending_restart_failure(
            candidate_ref=candidate_ref,
            error=error,
            old_runtime_recovered=old_runtime_recovered,
        )

    def retry_pending_restart(self, *, candidate_ref: str) -> ConfigReloadStatus:
        """为 Workspace pending candidate 创建新的受控重启 generation。"""

        return self._pending_restart.retry_pending_restart(candidate_ref=candidate_ref)

    def resolve_pending_restart(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
    ) -> ConfigReloadStatus:
        """用新 generation 的完整 proof 提升已成功启动的 Workspace candidate。"""

        return self._pending_restart.resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=health_proof,
        )

    def discard_pending_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
    ) -> ConfigReloadStatus:
        """仅在旧 active/source 基线可证明安全时丢弃 pending。"""

        return self._pending_restart.discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
        )

    @contextmanager
    def use_snapshot(self, snapshot: ConfigSnapshot) -> Iterator[None]:
        token = self._pinned_snapshot.set(snapshot)
        try:
            yield
        finally:
            self._pinned_snapshot.reset(token)

    def get_snapshot(self) -> ConfigSnapshot:
        return self._require_snapshot()

    def get_revision(self) -> str:
        return self._require_snapshot().revision

    def get_reload_status(self) -> ConfigReloadStatus:
        return self._reload_status.get_reload_status()


    def list_config_events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ):
        return self._event_cursor.list_config_events(after=after, limit=limit)

    def claim_config_events_for_consumer(
        self,
        *,
        after: int,
        consumer_id: str,
        limit: int = 100,
    ):
        return self._event_cursor.claim_config_events_for_consumer(
            after=after,
            consumer_id=consumer_id,
            limit=limit,
        )

    def mark_config_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ):
        return self._event_cursor.mark_config_event_delivered_for_consumer(
            event_id=event_id,
            consumer_id=consumer_id,
        )

    def ensure_config_event_cursor(self, *, after: int) -> None:
        self._event_cursor.ensure_config_event_cursor(after=after)

    def get_source_details(self) -> tuple[ConfigSource, ...]:
        return self._require_snapshot().source_details

    def get_source_diagnostics(
        self,
    ) -> tuple[str, Path, tuple[ConfigSource, ...]]:
        """只读返回配置来源诊断，不初始化快照或迁移 source layer。"""

        if self._snapshot_store.has_snapshot():
            snapshot = self._snapshot_store.current()
            return (
                snapshot.revision,
                snapshot.schema_path or self._resolve_schema_path(),
                snapshot.source_details,
            )
        if self._workspace_state_store is not None:
            active = self._workspace_state_store.get_active_config_snapshot(
                self._CONFIG_DOMAIN
            )
            if active is not None:
                source_details, _ = self._persisted_source_details(
                    active.source_baseline
                )
                return (
                    active.effective_digest,
                    self._resolve_schema_path(),
                    source_details,
                )

        source_details: list[ConfigSource] = [
            ConfigSource(
                path=self._inline_config_path,
                layer="inline",
                precedence=0,
                loaded=True,
            ),
            self._config_source(
                path=self._get_workspace_config_path(),
                layer="user",
                precedence=1,
                config_key="workspace_mutable_override",
            ),
            self._config_source(
                path=self._get_workspace_local_config_path(),
                layer="user_local",
                precedence=2,
                config_key="workspace_local_mutable_override",
            ),
        ]
        if self._workspace_root is not None:
            source_details.append(
                self._config_source(
                    path=get_workspace_config_path(self._workspace_root),
                    layer="workspace",
                    precedence=3,
                    config_key="workspace_root_mutable_override",
                )
            )
        if self._workspace_state_store is not None or self._runtime_config_overrides:
            source_details.append(self._runtime_override_source())
        return (
            "uninitialized",
            self._resolve_schema_path(),
            tuple(source_details),
        )

    def get_schema_path(self) -> Path:
        snapshot = self._require_snapshot()
        return snapshot.schema_path or self._resolve_schema_path()

    def get_runtime_override_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtime_config_overrides))

    def get_agent_run_timeout_seconds(self) -> float:
        """返回单个 Agent Job 的总执行上限。"""
        value = self._read_runtime_value(("agent", "run", "timeout_seconds"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                "runtime.agent.run.timeout_seconds 必须是正数"
            )
        if value <= 0:
            raise ValueError(
                "runtime.agent.run.timeout_seconds 必须大于 0"
            )
        return float(value)

    def get_agent_run_mode(self) -> str:
        """返回 Agent 运行模式，决定是否向模型暴露团队面板工具。"""
        value = self._read_runtime_value(("agent", "run", "mode"))
        if value not in {"single_agent", "team"}:
            raise ValueError(
                "runtime.agent.run.mode 必须是 single_agent 或 team"
            )
        return value

    def _read_runtime_value(self, path: tuple[str, ...]) -> object:
        current: object = self._get_effective_config().get("runtime", {})
        for key in path:
            if not isinstance(current, dict):
                raise ValueError(
                    f"运行时配置路径不是对象: runtime.{'.'.join(path)}"
                )
            if key not in current:
                raise ValueError(
                    f"内置运行时配置缺少参数: runtime.{'.'.join(path)}"
                )
            current = current[key]
        return current

    def get_debug_runtime_config(self) -> dict[str, Any]:
        """返回经过边界校验的源码调试运行时配置。"""
        runtime = self._get_effective_config().get("runtime", {})
        if runtime is None:
            runtime = {}
        if not isinstance(runtime, dict):
            raise ValueError("runtime 配置必须是对象")

        raw_debug = runtime.get("debug", {})
        if raw_debug is None:
            raw_debug = {}
        if not isinstance(raw_debug, dict):
            raise ValueError("runtime.debug 配置必须是对象")

        enabled = raw_debug.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("runtime.debug.enabled 必须是布尔值")

        allowed_adapters = {"node_inspector", "debugpy", "dap", "vscode"}
        default_adapter = raw_debug.get("default_adapter", "node_inspector")
        if default_adapter not in allowed_adapters:
            raise ValueError(
                "runtime.debug.default_adapter 不受支持: "
                f"{default_adapter!r}"
            )

        command_timeout_seconds = raw_debug.get("command_timeout_seconds", 10)
        if (
            isinstance(command_timeout_seconds, bool)
            or not isinstance(command_timeout_seconds, (int, float))
            or command_timeout_seconds <= 0
        ):
            raise ValueError(
                "runtime.debug.command_timeout_seconds 必须是正数"
            )

        node = self._parse_debug_endpoint_config(
            raw_debug.get("node"),
            namespace="runtime.debug.node",
            host_key="inspector_host",
            port_key="inspector_port",
            default_host="127.0.0.1",
        )
        executable = node.get("executable", "")
        if not isinstance(executable, str):
            raise ValueError("runtime.debug.node.executable 必须是字符串")
        node["executable"] = executable

        raw_python = raw_debug.get("python")
        if raw_python is None:
            raw_python = {}
        if not isinstance(raw_python, dict):
            raise ValueError("runtime.debug.python 配置必须是对象")
        python_adapter = raw_python.get("adapter", "debugpy")
        if python_adapter != "debugpy":
            raise ValueError(
                "runtime.debug.python.adapter 目前只支持 debugpy"
            )
        python = self._parse_debug_endpoint_config(
            raw_python,
            namespace="runtime.debug.python",
            host_key="debugpy_host",
            port_key="debugpy_port",
            default_host="127.0.0.1",
        )
        python["adapter"] = "debugpy"

        raw_profiles = raw_debug.get("launch_profiles", {})
        if raw_profiles is None:
            raw_profiles = {}
        if not isinstance(raw_profiles, dict):
            raise ValueError("runtime.debug.launch_profiles 配置必须是对象")
        profiles: dict[str, dict[str, Any]] = {}
        for profile_name, raw_profile in raw_profiles.items():
            if not isinstance(profile_name, str) or not profile_name:
                raise ValueError("runtime.debug.launch_profiles 的名称必须是非空字符串")
            if not isinstance(raw_profile, dict):
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name} 必须是对象"
                )
            profile_adapter = raw_profile.get("adapter", default_adapter)
            if profile_adapter not in allowed_adapters:
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name}.adapter 不受支持: "
                    f"{profile_adapter!r}"
                )
            profile_runtime = raw_profile.get(
                "runtime",
                "node" if profile_adapter == "node_inspector" else "",
            )
            if not isinstance(profile_runtime, str) or not profile_runtime.strip():
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.runtime 必须是非空字符串"
                )
            profile_program = raw_profile.get("program", "")
            if not isinstance(profile_program, str):
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name}.program 必须是字符串"
                )
            working_directory = raw_profile.get("working_directory", "")
            if not isinstance(working_directory, str):
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.working_directory 必须是字符串"
                )
            args = raw_profile.get("args", [])
            if not isinstance(args, list) or len(args) > 20 or not all(
                isinstance(argument, str) for argument in args
            ):
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.args 必须是最多 20 个字符串的数组"
                )
            profiles[profile_name] = {
                "adapter": profile_adapter,
                "runtime": profile_runtime,
                "program": profile_program,
                "working_directory": working_directory,
                "args": list(args),
            }

        if not profiles:
            default_runtime = {
                "node_inspector": "node",
                "debugpy": "python",
                "dap": "",
                "vscode": "vscode",
            }[default_adapter]
            profiles["node-default"] = {
                "adapter": default_adapter,
                "runtime": default_runtime,
                "program": "",
                "working_directory": "",
                "args": [],
            }

        return {
            "enabled": enabled,
            "default_adapter": default_adapter,
            "command_timeout_seconds": float(command_timeout_seconds),
            "node": node,
            "python": python,
            "launch_profiles": profiles,
        }

    @staticmethod
    def _parse_debug_endpoint_config(
        raw_value: object,
        *,
        namespace: str,
        host_key: str,
        port_key: str,
        default_host: str,
    ) -> dict[str, Any]:
        if raw_value is None:
            raw_value = {}
        if not isinstance(raw_value, dict):
            raise ValueError(f"{namespace} 配置必须是对象")
        host = raw_value.get(host_key, default_host)
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"{namespace}.{host_key} 必须是非空字符串")
        if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            raise ValueError(
                f"{namespace}.{host_key} 必须是 loopback 地址，实际值: {host!r}"
            )
        port = raw_value.get(port_key, 0)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError(
                f"{namespace}.{port_key} 必须是 0-65535 的整数"
            )
        return {host_key: host, port_key: port}

    def get_gateway_connection_url(self) -> str:
        value = self._read_runtime_value(("gateway", "connection", "url"))
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runtime.gateway.connection.url 必须是非空字符串")
        return value

    def get_gateway_connection_timeout_seconds(self) -> float:
        value = self._read_runtime_value(
            ("gateway", "connection", "timeout_seconds")
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                "runtime.gateway.connection.timeout_seconds 必须是正数"
            )
        return float(value)

    def get_terminal_backend_url(self) -> str:
        value = self._read_runtime_value(
            ("auxiliary_services", "terminal_backend", "url")
        )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "runtime.auxiliary_services.terminal_backend.url 必须是非空字符串"
            )
        return value

    def get_browser_backend_url(self) -> str:
        value = self._read_runtime_value(
            ("auxiliary_services", "browser_backend", "url")
        )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "runtime.auxiliary_services.browser_backend.url 必须是非空字符串"
            )
        return value

    def get_workspace_file_default_limit(self) -> int:
        value = self._read_runtime_value(
            ("workspace", "files", "list", "default_limit")
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > 1000
        ):
            raise ValueError(
                "runtime.workspace.files.list.default_limit 必须是 1-1000 的整数"
            )
        return value

    def get_workspace_preview_max_bytes(self) -> int:
        value = self._read_runtime_value(("workspace", "files", "preview", "max_bytes"))
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "runtime.workspace.files.preview.max_bytes 必须是正整数"
            )
        return value

    def get_workspace_preview_binary_sample_bytes(self) -> int:
        value = self._read_runtime_value(
            ("workspace", "files", "preview", "binary_sample_bytes")
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "runtime.workspace.files.preview.binary_sample_bytes 必须是正整数"
            )
        return value

    def get_trace_stream_heartbeat_interval_seconds(self) -> float:
        value = self._read_runtime_value(
            ("events", "trace_stream", "heartbeat_interval_seconds")
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                "runtime.events.trace_stream.heartbeat_interval_seconds 必须是正数"
            )
        return float(value)

    def config_from_snapshot(self, snapshot: ConfigSnapshot) -> dict[str, Any]:
        return snapshot.to_dict()

    def validate_candidate(
        self,
        snapshot: ConfigSnapshot,
        *,
        mcp_tool_names: frozenset[str],
    ) -> None:
        config = snapshot.to_dict()
        self._validate_agent_tool_policies(
            config,
            mcp_tool_names=mcp_tool_names,
        )

    def _validate_shadow_config(self, config: Mapping[str, object]) -> None:
        """shadow generation 的纯内存 schema 校验入口。

        source 文件稳定读取和工具策略预检仍由 candidate builder 负责；这里
        禁止重读磁盘，否则 bootstrap 会把尚未构建的坏 candidate 误算成旧
        active generation 的失败。
        """
        jsonschema.validate(dict(config), self._load_schema())

    @staticmethod
    async def _reconcile_shadow_config(
        _config: Mapping[str, object],
        _scope: LifetimeScope,
    ) -> None:
        """配置 source 已由 ConfigService 读取；运行时 reconcile 在发布钩子执行。"""

    async def _ensure_shadow_started(self) -> None:
        readiness = self._shadow_lifecycle.readiness
        if readiness == "ready":
            return
        if readiness != "cold":
            raise ConfigShadowLifecycleError(
                "readiness_gate_closed",
                f"workspace 配置 shadow lifecycle 不可启动: readiness={readiness}",
            )
        await self._shadow_lifecycle.bootstrap(self._require_snapshot().to_dict())

    async def _renew_apply_claim(self, apply_id: str, fencing_token: str) -> None:
        """在外部副作用期间续租 claim；claim 丢失由最终 CAS 明确暴露。"""

        while True:
            await asyncio.sleep(10)
            if self._workspace_state_store is None:
                return
            await asyncio.to_thread(
                self._workspace_state_store.renew_config_apply_claim,
                config_domain=self._CONFIG_DOMAIN,
                apply_id=apply_id,
                fencing_token=fencing_token,
                lease_seconds=30,
            )

    async def reload(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        async def persist_candidate(
            previous: ConfigSnapshot,
            candidate: ConfigSnapshot,
        ) -> None:
            pending = self._prepare_candidate(
                previous,
                candidate,
                idempotency_key=idempotency_key,
            )
            changed_paths = changed_json_paths(previous.to_dict(), candidate.to_dict())
            attempt_id = new_config_id("attempt")
            apply_id = new_config_id("apply")
            active = (
                self._workspace_state_store.get_active_config_snapshot(
                    self._CONFIG_DOMAIN
                )
                if self._workspace_state_store is not None
                else None
            )
            base_active_revision = active.active_revision if active is not None else None
            claim = None
            claim_renewal_task: asyncio.Task[None] | None = None
            if self._workspace_state_store is not None:
                claim = self._workspace_state_store.begin_config_apply(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    owner="workspace-config-service",
                    base_active_revision=base_active_revision,
                    target_generation=new_config_id("workspace_generation"),
                    pending_revision=pending.pending_revision,
                    source_baseline=pending.source_baseline,
                    active_baseline=(
                        active.source_baseline if active is not None else {}
                    ),
                )
                claim_renewal_task = asyncio.create_task(
                    self._renew_apply_claim(claim.apply_id, claim.fencing_token)
                )
            try:
                if candidate_applier is not None:
                    await candidate_applier(previous, candidate)
                    if self._workspace_state_store is not None and claim is not None:
                        self._workspace_state_store.append_config_apply_side_effect(
                            apply_id=claim.apply_id,
                            side_effect={
                                "resource": "workspace-config-applier",
                                "action": "apply",
                                "candidate_id": pending.candidate_id,
                                "changed_paths": list(changed_paths),
                            },
                        )
            except ConfigRestartRequiredError as error:
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state="pending_restart",
                    result="restart_required",
                    error=str(error),
                    changed_paths=changed_paths,
                    candidate_ref=new_config_id("candidate_ref"),
                    target_generation=(claim.target_generation if claim else None),
                    fencing_token=(claim.fencing_token if claim else None),
                )
                if self._workspace_state_store is not None and claim is not None:
                    if claim_renewal_task is not None:
                        claim_renewal_task.cancel()
                        await asyncio.gather(
                            claim_renewal_task,
                            return_exceptions=True,
                        )
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state="failed",
                        last_error=str(error),
                    )
                    self._workspace_state_store.release_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                        apply_id=claim.apply_id,
                        fencing_token=claim.fencing_token,
                    )
                raise
            except ConfigConflictError as error:
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state="conflict",
                    result="conflict",
                    error=str(error),
                    changed_paths=changed_paths,
                )
                if self._workspace_state_store is not None and claim is not None:
                    if claim_renewal_task is not None:
                        claim_renewal_task.cancel()
                        await asyncio.gather(
                            claim_renewal_task,
                            return_exceptions=True,
                        )
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state="failed",
                        last_error=str(error),
                    )
                    self._workspace_state_store.release_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                        apply_id=claim.apply_id,
                        fencing_token=claim.fencing_token,
                    )
                raise
            except Exception as error:
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state="rejected",
                    result="apply_failed",
                    error=str(error),
                    changed_paths=changed_paths,
                )
                if self._workspace_state_store is not None and claim is not None:
                    if claim_renewal_task is not None:
                        claim_renewal_task.cancel()
                        await asyncio.gather(
                            claim_renewal_task,
                            return_exceptions=True,
                        )
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state="failed",
                        last_error=str(error),
                    )
                    self._workspace_state_store.release_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                        apply_id=claim.apply_id,
                        fencing_token=claim.fencing_token,
                    )
                raise
            try:
                pending_layer_revisions = {
                    str(key): int(detail["layer_revision"])
                    for key, detail in pending.source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                pending_layer_digests = {
                    str(key): (
                        str(detail.get("layer_digest"))
                        if detail.get("layer_digest") is not None
                        else None
                    )
                    for key, detail in pending.source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                self._persist_active_snapshot(
                    candidate,
                    candidate_id=pending.candidate_id,
                    expected_active_revision=base_active_revision,
                    expected_pending_revision=pending.pending_revision,
                    expected_pending_state="applying",
                    expected_fencing_token=(claim.fencing_token if claim else None),
                    expected_source_baseline=pending.source_baseline,
                    expected_source_generation=pending.source_generation,
                    expected_layer_revisions=pending_layer_revisions,
                    expected_layer_digests=pending_layer_digests,
                    apply_id=apply_id,
                    event=ConfigEventInput(
                        event_id=f"config:{pending.candidate_id}:active",
                        config_domain=self._CONFIG_DOMAIN,
                        candidate_id=pending.candidate_id,
                        attempt_id=attempt_id,
                        apply_id=apply_id,
                        idempotency_key=pending.idempotency_key,
                        commit_revision=None,
                        active_revision=None,
                        pending_revision=pending.pending_revision,
                        source="workspace-config-service",
                        result="applied",
                        activation_scope=workspace_config_policy().activation_scope_for(
                            changed_paths
                        ),
                        changed_paths=changed_paths,
                        applied_paths=changed_paths,
                    ),
                )
            except ConfigConflictError as error:
                if self._workspace_state_store is not None and claim is not None:
                    journal = self._workspace_state_store.get_config_apply_journal(
                        apply_id=claim.apply_id
                    )
                    recovery_required = bool(journal and journal.side_effects)
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=str(error),
                    )
                else:
                    recovery_required = False
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "conflict"),
                    result=("recovery_required" if recovery_required else "conflict"),
                    error=str(error),
                    changed_paths=changed_paths,
                )
                raise
            except Exception as error:
                if self._workspace_state_store is not None and claim is not None:
                    journal = self._workspace_state_store.get_config_apply_journal(
                        apply_id=claim.apply_id
                    )
                    recovery_required = bool(journal and journal.side_effects)
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=str(error),
                    )
                else:
                    recovery_required = False
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "rejected"),
                    result=("recovery_required" if recovery_required else "apply_failed"),
                    error=str(error),
                    changed_paths=changed_paths,
                )
                raise
            finally:
                if claim_renewal_task is not None:
                    claim_renewal_task.cancel()
                    await asyncio.gather(
                        claim_renewal_task,
                        return_exceptions=True,
                    )
                if self._workspace_state_store is not None and claim is not None:
                    self._workspace_state_store.release_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                        apply_id=claim.apply_id,
                        fencing_token=claim.fencing_token,
                    )

        async def apply_through_shadow(
            previous: ConfigSnapshot,
            candidate: ConfigSnapshot,
        ) -> None:
            # candidate builder 已成功后才启动 shadow；解析/schema 失败仍由
            # ConfigSnapshotStore 记录本次 reload failure，不能在其外层短路。
            if self._shadow_lifecycle.readiness == "cold":
                await self._shadow_lifecycle.bootstrap(previous.to_dict())
            publish_hook = None
            if candidate.revision != previous.revision:

                async def publish_candidate(_generation) -> None:
                    await persist_candidate(previous, candidate)

                publish_hook = publish_candidate
            await self._shadow_lifecycle.apply_candidate(
                candidate.to_dict(),
                expected_generation=self._shadow_lifecycle.generation,
                publish_hook=publish_hook,
            )

        return await self._snapshot_store.reload(
            candidate_applier=apply_through_shadow,
            apply_unchanged=True,
        )

    def _prepare_candidate(
        self,
        previous: ConfigSnapshot,
        snapshot: ConfigSnapshot,
        *,
        idempotency_key: str | None = None,
    ) -> ConfigPendingCandidateRecord:
        if self._workspace_state_store is None:
            payload = redact_config_payload(snapshot.to_dict())
            if not isinstance(payload, dict):
                raise TypeError("脱敏配置候选必须是对象")
            return ConfigPendingCandidateRecord(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=f"candidate_{snapshot.revision}",
                idempotency_key=idempotency_key or f"reload:{snapshot.revision}",
                pending_revision=0,
                payload=payload,
                source_baseline={},
                candidate_digest=snapshot.revision,
                effective_digest=snapshot.revision,
                target_generation=None,
                fencing_token=None,
                state="candidate_validated",
                last_error=None,
                created_at=snapshot.loaded_at,
            )
        baseline, source_generation, _, _ = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置候选必须是对象")
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        candidate_id = f"candidate_{source_generation}_{snapshot.revision}"
        pending = self._workspace_state_store.create_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=candidate_id,
            idempotency_key=(
                idempotency_key or f"reload:{source_generation}:{snapshot.revision}"
            ),
            payload=payload,
            source_baseline=baseline,
            candidate_digest=snapshot.revision,
            effective_digest=snapshot.revision,
            target_generation="workspace-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=(active.active_revision if active is not None else None),
            source_generation=source_generation,
        )
        if pending.source_baseline != baseline:
            raise ConfigConflictError(
                "重复 candidate 的 source baseline 与当前候选不一致"
            )
        if pending.state in {"rejected", "conflict", "recovery_required"}:
            pending = self._workspace_state_store.update_pending_config_candidate_state(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                expected_state=pending.state,
                state="candidate_validated",
                last_error=None,
            )
        elif pending.state != "candidate_validated":
            raise ConfigConflictError(
                "重复 candidate 已经处于不可自动重试状态: "
                f"candidate={pending.candidate_id}, state={pending.state}"
            )
        return pending

    def _finish_candidate(
        self,
        pending: ConfigPendingCandidateRecord,
        *,
        attempt_id: str,
        apply_id: str,
        state: ConfigLifecycleState,
        result: ConfigResult,
        error: str,
        changed_paths: tuple[str, ...] = (),
        candidate_ref: str | None = None,
        target_generation: str | None = None,
        fencing_token: str | None = None,
    ) -> None:
        if self._workspace_state_store is None:
            return
        event_id = f"config:{pending.candidate_id}:{result}"
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        self._workspace_state_store.update_pending_config_candidate_state(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            expected_state="applying",
            state=state,
            last_error=error,
            candidate_ref=candidate_ref,
            target_generation=target_generation,
            fencing_token=fencing_token,
            event=ConfigEventInput(
                event_id=event_id,
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision if active is not None else None,
                pending_revision=pending.pending_revision,
                source="workspace-config-service",
                result=result,
                activation_scope=workspace_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
                deferred_paths=(
                    changed_paths if result == "restart_required" else ()
                ),
                error=error,
            ),
        )

    def _source_baseline(
        self,
        snapshot: ConfigSnapshot,
    ) -> tuple[dict[str, object], int, dict[str, int], dict[str, str | None]]:
        baseline: dict[str, object] = {}
        layer_revisions: dict[str, int] = {}
        layer_digests: dict[str, str | None] = {}
        source_generation = 0
        for source in snapshot.source_details:
            layer_key = source.source_key or f"{source.layer}:{source.precedence}"
            source_path = source.path
            if self._workspace_state_store is not None and source.source_key is not None:
                stored_source = self._workspace_state_store.get_source_layer(
                    source.source_key
                )
                if stored_source is not None:
                    source_path = Path(stored_source.source_path)
            baseline[layer_key] = {
                "path": str(source_path),
                "presence": source.presence,
                "layer_revision": source.layer_revision,
                "layer_digest": source.layer_digest,
                "source_generation": source.source_generation,
            }
            if source.layer_revision is not None:
                layer_revisions[layer_key] = source.layer_revision
            layer_digests[layer_key] = source.layer_digest
            if source.source_generation is not None:
                source_generation = max(source_generation, source.source_generation)
        return baseline, source_generation, layer_revisions, layer_digests

    def _persist_initial_active_snapshot(self) -> None:
        if self._workspace_state_store is None:
            return
        snapshot = self.get_snapshot()
        baseline, source_generation, layer_revisions, layer_digests = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置快照必须是对象")
        self._workspace_state_store.ensure_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=snapshot.revision,
            secret_bindings=build_secret_binding_summary(snapshot.to_dict()),
            schema_version=1,
            promoted_generation="bootstrap",
        )

    def _persist_active_snapshot(
        self,
        snapshot: ConfigSnapshot,
        *,
        candidate_id: str | None = None,
        expected_active_revision: int | None = None,
        expected_pending_revision: int | None = None,
        expected_pending_state: ConfigLifecycleState | None = None,
        expected_fencing_token: str | None = None,
        expected_source_baseline: dict[str, object] | None = None,
        expected_source_generation: int | None = None,
        expected_layer_revisions: dict[str, int] | None = None,
        expected_layer_digests: dict[str, str | None] | None = None,
        apply_id: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> None:
        if self._workspace_state_store is None:
            return
        baseline, source_generation, layer_revisions, layer_digests = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置快照必须是对象")
        self._workspace_state_store.promote_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=candidate_id or f"candidate_{snapshot.revision}",
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=snapshot.revision,
            secret_bindings=build_secret_binding_summary(snapshot.to_dict()),
            schema_version=1,
            promoted_generation="workspace-runtime",
            promoted_apply_id=apply_id,
            expected_active_revision=expected_active_revision,
            expected_source_generation=(
                expected_source_generation
                if expected_source_generation is not None
                else source_generation
            ),
            expected_source_baseline=expected_source_baseline,
            expected_layer_revisions=(
                expected_layer_revisions
                if expected_layer_revisions is not None
                else layer_revisions
            ),
            expected_layer_digests=(
                expected_layer_digests
                if expected_layer_digests is not None
                else layer_digests
            ),
            expected_pending_revision=expected_pending_revision,
            expected_pending_state=expected_pending_state,
            expected_fencing_token=expected_fencing_token,
            event=event,
        )

    async def start_watching(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
    ) -> None:
        if self._watcher is not None:
            raise RuntimeError("配置文件监听器不允许重复启动")
        await self._ensure_shadow_started()
        self._candidate_applier = candidate_applier
        directories = {self._get_workspace_config_path().parent}
        candidate_paths = {
            self._get_workspace_config_path(),
            self._get_workspace_local_config_path(),
        }
        if self._workspace_root is not None:
            workspace_config_dir = self._workspace_root / ".boxteam"
            directories.add(workspace_config_dir)
            candidate_paths.add(workspace_config_dir / "workspace.jsonc")
        watcher = ConfigFileWatcher(
            directories=directories,
            candidate_paths=candidate_paths,
            on_change=lambda: self._reload_from_watcher(
                candidate_applier=candidate_applier,
            ),
        )
        await watcher.start()
        self._watcher = watcher

    async def stop_watching(self) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is None:
            self._candidate_applier = None
            return
        await watcher.stop()
        self._candidate_applier = None

    async def close(self) -> None:
        """停止文件监听并关闭 workspace 配置 shadow generation。"""
        await self.stop_watching()
        await self._shadow_lifecycle.close()

    async def _reload_from_watcher(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None,
    ) -> None:
        try:
            await self.reload(candidate_applier=candidate_applier)
        except Exception as error:
            # reload 已写入失败状态并发布 failed 事件；watcher 继续等待后续修复，
            # 同时必须记录完整堆栈，不能把失败静默转换成成功。
            logger.exception("workspace 配置 watcher reload 失败", exc_info=error)
            return

    def _get_effective_config(self) -> dict[str, Any]:
        return self._require_snapshot().to_dict()

    async def get(self) -> ConfigDTO:
        return self._build_public_config()

    async def update(self, payload: ConfigUpdateRequest) -> ConfigDTO:
        runtime_keys = frozenset(
            {
                "default_model",
                "default_orchestration",
                "max_concurrent_agents",
                "allow_shell_tools",
                "ignored_paths",
                "auto_summarize",
            }
        )
        update_data = {
            key: value
            for key, value in payload.model_dump(exclude_unset=True).items()
            if key in runtime_keys
        }
        if self._workspace_state_store is not None:
            required_cas_fields = {
                "base_layer_revision",
                "base_layer_digest",
                "expected_active_revision",
                "expected_active_digest",
            }
            missing_cas_fields = required_cas_fields.difference(
                payload.model_fields_set
            )
            if missing_cas_fields:
                raise ConfigConflictError(
                    "配置 API/UI 写入必须声明完整 CAS 基线，缺少: "
                    + ", ".join(sorted(missing_cas_fields))
                )
            existing = self._workspace_state_store.get_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN,
                idempotency_key=payload.idempotency_key,
            )
            if (
                existing is not None
                and existing.state in {"active", "pending_restart"}
                and self._is_replayed_runtime_update(
                    existing,
                    updates=update_data,
                )
            ):
                return self._build_public_config()
        self._sync_runtime_override_source(
            updates=update_data,
            base_layer_revision=payload.base_layer_revision,
            base_layer_digest=payload.base_layer_digest,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
        if self._snapshot_store.has_snapshot():
            await self.reload(
                candidate_applier=self._candidate_applier,
                idempotency_key=payload.idempotency_key,
            )
        return self._build_public_config()

    def _is_replayed_runtime_update(
        self,
        pending: ConfigPendingCandidateRecord,
        *,
        updates: dict[str, Any],
    ) -> bool:
        """只对同一 source 基线和同一 runtime payload 的重试做幂等短路。"""

        if self._workspace_state_store is None:
            return False
        baseline = pending.source_baseline.get(self._RUNTIME_OVERRIDE_CONFIG_KEY)
        source = self._workspace_state_store.get_source_layer(
            self._RUNTIME_OVERRIDE_CONFIG_KEY
        )
        if not isinstance(baseline, dict) or source is None:
            return False
        if (
            baseline.get("path") != source.source_path
            or baseline.get("presence") != source.presence
            or baseline.get("layer_revision") != source.layer_revision
            or baseline.get("layer_digest") != source.layer_digest
            or baseline.get("source_generation") != source.source_generation
        ):
            return False
        current_payload = source.payload or {}
        return all(
            key not in current_payload if value is None else current_payload.get(key) == value
            for key, value in updates.items()
        )

    def _build_public_config(self) -> ConfigDTO:
        config = self._get_effective_config()
        snapshot = self._require_snapshot()
        reload_status = self.get_reload_status()
        public_config = self._load_public_runtime_config(config)

        default_model = public_config.get("default_model")
        if default_model is None:
            default_model = self._llm_resolution.resolve_default_model(config)

        default_orchestration = public_config.get(
            "default_orchestration", "single_agent"
        )
        max_concurrent_agents = public_config.get("max_concurrent_agents", 4)
        allow_shell_tools = public_config.get("allow_shell_tools", False)
        ignored_paths = public_config.get("ignored_paths", [])
        auto_summarize = public_config.get("auto_summarize", True)

        return ConfigDTO(
            default_model=default_model,
            default_orchestration=default_orchestration,
            max_concurrent_agents=max_concurrent_agents,
            allow_shell_tools=allow_shell_tools,
            ignored_paths=ignored_paths,
            auto_summarize=auto_summarize,
            metadata={
                "default_agent_id": self.get_default_agent_id(),
                "config_path": str(self._get_workspace_config_path()),
                "source": "workspace",
                "runtime_overrides": sorted(self._runtime_config_overrides.keys()),
                "revision": snapshot.revision,
                "source_paths": [str(path) for path in snapshot.source_paths],
                "source_details": [
                    {
                        "path": str(source.path),
                        "layer": source.layer,
                        "precedence": source.precedence,
                        "loaded": source.loaded,
                        "source_key": source.source_key,
                        "presence": source.presence,
                        "layer_revision": source.layer_revision,
                        "layer_digest": source.layer_digest,
                        "source_generation": source.source_generation,
                    }
                    for source in snapshot.source_details
                ],
                "policy_manifest": list(
                    workspace_config_policy().policy_manifest()
                ),
                "reload": {
                    "healthy": reload_status.healthy,
                    "restart_required": reload_status.restart_required,
                    "reason": reload_status.reason,
                    "changed_sections": list(reload_status.changed_sections),
                    "last_success_at": reload_status.last_success_at.isoformat(),
                    "last_attempt_at": reload_status.last_attempt_at.isoformat(),
                    "last_error": reload_status.last_error,
                    "state": reload_status.state,
                    "active_revision": reload_status.active_revision,
                    "pending_revision": reload_status.pending_revision,
                    "candidate_id": reload_status.candidate_id,
                    "attempt_id": reload_status.attempt_id,
                    "apply_id": reload_status.apply_id,
                    "layer_digests": reload_status.layer_digests or {},
                    "applied_paths": list(reload_status.applied_paths),
                    "deferred_paths": list(reload_status.deferred_paths),
                },
            },
        )

    def _load_public_runtime_config(self, config: dict[str, Any]) -> dict[str, Any]:
        raw_public_config = config.get("ui", {})
        if raw_public_config is None:
            raw_public_config = {}
        if not isinstance(raw_public_config, dict):
            raise ValueError("ui 配置必须是对象")
        return {**raw_public_config, **self._runtime_config_overrides}

    def get_llm_providers(self) -> list[dict]:
        return self._llm_resolution.get_llm_providers()

    def get_llm_provider(self, provider_id: str) -> dict[str, Any]:
        return self._llm_resolution.get_llm_provider(provider_id)


    def get_default_agent_runtime_config(self) -> dict[str, Any]:
        return self.get_agent_runtime_config(self.get_default_agent_id())

    def get_default_agent_id(self) -> str:
        config = self._get_effective_config()
        default_agent_id = config.get("default_agent")
        agents = config.get("agents", {})

        if default_agent_id and default_agent_id in agents:
            return default_agent_id

        return "default"

    def get_workspace_default_agent_id(self) -> str:
        return self._session_defaults.get_workspace_default_agent_id()

    def get_workspace_default_provider_id(self, agent_id: str) -> str:
        return self._session_defaults.get_workspace_default_provider_id(agent_id)

    def set_workspace_default_agent(self, agent_id: str) -> None:
        self._session_defaults.set_workspace_default_agent(agent_id)

    def set_workspace_default_provider(
        self,
        agent_id: str,
        provider_id: str,
    ) -> None:
        self._session_defaults.set_workspace_default_provider(agent_id, provider_id)

    def resolve_new_session_agent_id(self, agent_id: str | None) -> str:
        return self._session_defaults.resolve_new_session_agent_id(agent_id)

    def resolve_new_session_provider_id(self, agent_id: str) -> str:
        return self._session_defaults.resolve_new_session_provider_id(agent_id)


    def _normalize_agent_id(self, agent_id: str | None) -> str:
        if not agent_id:
            return self.get_default_agent_id()

        # TODO: 兼容历史别名 deep_agent，后续移除
        if agent_id == "deep_agent":
            return self.get_default_agent_id()

        return agent_id

    def resolve_agent_id(self, agent_id: str | None) -> str:
        return self._normalize_agent_id(agent_id)

    def validate_agent_id(self, agent_id: str | None) -> str:
        resolved_agent_id = self._normalize_agent_id(agent_id)
        config = self._get_effective_config()
        agents = config.get("agents", {})

        if not agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return resolved_agent_id

        if resolved_agent_id not in agents:
            raise ValueError(f"agent {resolved_agent_id} 不存在")

        return resolved_agent_id

    def list_agents(self) -> dict[str, dict[str, Any]]:
        config = self._get_effective_config()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        return agents

    def get_logger_level(self) -> str:
        config = self._get_effective_config()
        logger_config = config.get("logger", {})
        if not isinstance(logger_config, dict):
            raise ValueError("logger 配置必须是对象")
        level = logger_config.get("level", "info")
        if not isinstance(level, str):
            raise ValueError("logger.level 必须是字符串")
        normalized_level = level.strip().upper()
        if normalized_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError(
                "logger.level 仅支持 debug、info、warning、error、critical"
            )
        return normalized_level

    def get_logger_pretty(self) -> bool:
        config = self._get_effective_config()
        logger_config = config.get("logger", {})
        if not isinstance(logger_config, dict):
            raise ValueError("logger 配置必须是对象")
        pretty = logger_config.get("pretty", True)
        if not isinstance(pretty, bool):
            raise ValueError("logger.pretty 必须是布尔值")
        return pretty

    def development_test_tools_enabled(self) -> bool:
        config = self._get_effective_config()
        development = config.get("development", {})
        if development is None:
            return False
        if not isinstance(development, dict):
            raise ValueError("development 配置必须是对象")
        enabled = development.get("test_tools", False)
        if not isinstance(enabled, bool):
            raise ValueError("development.test_tools 必须是布尔值")
        return enabled

    def get_agent_runtime_config(
        self,
        agent_id: str | None = None,
        preferred_provider_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._get_effective_config()
        providers = self.get_llm_providers()

        if not providers:
            raise ValueError("未配置任何 LLM provider")

        default_runtime = {
            "system_prompt": "You are a helpful assistant.",
            "providers": providers,
            "require_delegated_report": False,
        }

        agents = config.get("agents", {})

        resolved_agent_id = self._normalize_agent_id(agent_id)
        if not agents or resolved_agent_id not in agents:
            if resolved_agent_id != "default":
                raise ValueError(f"agent {resolved_agent_id} 不存在")
            return default_runtime

        target_agent = agents[resolved_agent_id]
        instructions = target_agent.get("instructions", {})
        model_cfg = target_agent.get("model", {})
        execution_cfg = target_agent.get("execution", {})
        if not isinstance(execution_cfg, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 execution 配置必须是对象")
        require_delegated_report = execution_cfg.get(
            "require_delegated_report",
            False,
        )
        if not isinstance(require_delegated_report, bool):
            raise TypeError(
                f"agent {resolved_agent_id} 的 "
                "execution.require_delegated_report 必须是布尔值"
            )

        provider_map: dict[str, dict[str, Any]] = {}
        for index, provider in enumerate(providers):
            provider_id = provider.get("id")
            if not provider_id:
                provider_id = f"provider_{index}"
            provider_map[provider_id] = provider

        primary_provider = model_cfg.get("primary_provider")
        fallback_providers = model_cfg.get("fallback_providers", [])

        if not primary_provider:
            raise ValueError(
                f"agent {resolved_agent_id} 缺少 model.primary_provider 配置"
            )

        provider_ids = [primary_provider, *fallback_providers]
        if preferred_provider_id is not None:
            if preferred_provider_id not in provider_ids:
                raise ValueError(
                    f"agent {resolved_agent_id} 不允许使用 provider: "
                    f"{preferred_provider_id}"
                )
            provider_ids = [
                preferred_provider_id,
                *(item for item in provider_ids if item != preferred_provider_id),
            ]
        selected_providers = []
        for provider_id in provider_ids:
            provider = provider_map.get(provider_id)
            if provider is None:
                raise ValueError(
                    f"agent {resolved_agent_id} 引用了不存在的 provider: {provider_id}"
                )
            selected_providers.append(provider)

        runtime_config: dict[str, Any] = {
            "system_prompt": instructions.get(
                "system_prompt", default_runtime["system_prompt"]
            ),
            "providers": selected_providers,
            "require_delegated_report": require_delegated_report,
        }
        for option_name in ("temperature", "top_p", "max_output_tokens"):
            if option_name in model_cfg:
                runtime_config[option_name] = model_cfg[option_name]
        return runtime_config

    def resolve_agent_provider_id(
        self,
        agent_id: str | None,
        provider_id: str | None = None,
    ) -> str:
        runtime = self.get_agent_runtime_config(
            agent_id,
            preferred_provider_id=provider_id,
        )
        providers = runtime["providers"]
        if not providers:
            raise ValueError(
                f"agent {self._normalize_agent_id(agent_id)} 没有可用 provider"
            )
        resolved = providers[0].get("id")
        if not isinstance(resolved, str) or not resolved:
            raise ValueError("LLM provider 缺少非空 id")
        return resolved

    def get_agent_tool_config(self, agent_id: str | None = None) -> dict[str, Any]:
        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        return self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )

    def get_tool_policy_resolver(
        self,
        agent_id: str | None = None,
    ) -> ToolPolicyResolver:
        """返回当前 Workspace 和 Agent 合并后的唯一工具策略解析器。"""

        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tooling = config.get("tooling", {})
        if tooling is None:
            tooling = {}
        if not isinstance(tooling, dict):
            raise TypeError("tooling 配置必须是对象")

        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise TypeError("agents 配置必须是对象")
        agent_config = agents.get(resolved_agent_id, {})
        if not isinstance(agent_config, dict):
            raise TypeError(f"agent {resolved_agent_id} 配置必须是对象")
        agent_tools = agent_config.get("tools", {})
        if agent_tools is None:
            agent_tools = {}
        if not isinstance(agent_tools, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools 配置必须是对象")
        agent_policy = agent_tools.get("policy", {})
        if agent_policy is None:
            agent_policy = {}
        if not isinstance(agent_policy, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools.policy 必须是对象")

        defaults = tooling.get("policy_defaults", {})
        if not isinstance(defaults, dict):
            raise TypeError("tooling.policy_defaults 必须是对象")
        global_rules = tooling.get("policy_rules", {})
        if not isinstance(global_rules, dict):
            raise TypeError("tooling.policy_rules 必须是对象")
        agent_rules = agent_policy.get("rules", {})
        if not isinstance(agent_rules, dict):
            raise TypeError(f"agent {resolved_agent_id} 的 tools.policy.rules 必须是对象")
        rules = merge_json_objects(global_rules, agent_rules)

        global_restrictions = tooling.get("restrictions", {})
        if not isinstance(global_restrictions, dict):
            raise TypeError("tooling.restrictions 必须是对象")
        agent_restrictions = agent_policy.get("restrictions", {})
        if not isinstance(agent_restrictions, dict):
            raise TypeError(
                f"agent {resolved_agent_id} 的 tools.policy.restrictions 必须是对象"
            )
        restrictions = dict(global_restrictions)
        for name in (
            "execution_disabled",
            "model_hidden",
            "confirmation_required",
        ):
            merged = list(global_restrictions.get(name, []))
            merged.extend(agent_restrictions.get(name, []))
            restrictions[name] = list(dict.fromkeys(merged))

        # 现有 allowlist/denylist 和 confirmation_required 继续作为静态限制，
        # 但统一转换到 ToolPolicyResolver，不再由各个运行时调用方分别解释。
        legacy_policy = self.resolve_agent_tool_policy(resolved_agent_id)
        restrictions["execution_disabled"] = list(
            dict.fromkeys(
                [
                    *restrictions.get("execution_disabled", []),
                    *legacy_policy.disabled_names,
                ]
            )
        )
        legacy_tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        restrictions["confirmation_required"] = list(
            dict.fromkeys(
                [
                    *restrictions.get("confirmation_required", []),
                    *legacy_tool_config["confirmation_required"],
                ]
            )
        )
        return ToolPolicyResolver(
            policy_defaults=defaults,
            policy_rules=rules,
            restrictions=restrictions,
        )

    def get_mcp_config(self) -> dict[str, Any]:
        config = self._get_effective_config()
        raw_mcp_config = config.get("mcp", {})
        if not isinstance(raw_mcp_config, dict):
            raise TypeError("mcp 配置必须是对象")
        return dict(raw_mcp_config)

    def set_mcp_tool_names(self, tool_names: frozenset[str]) -> None:
        """注册当前进程实际发现的 MCP 工具，并重新严格校验工具策略。"""

        previous_tool_names = self._mcp_tool_names
        self._mcp_tool_names = frozenset(tool_names)
        try:
            self._validate_agent_tool_policies(self._get_effective_config())
        except Exception:
            self._mcp_tool_names = previous_tool_names
            raise

    def resolve_agent_tool_policy(
        self,
        agent_id: str | None = None,
    ) -> ResolvedToolPolicy:
        """返回配置、目录展示和运行时共同使用的权威工具策略。"""

        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        custom_tool_names = custom_tool_spec_names(
            tool_config["custom"],
            context=f"agent {resolved_agent_id} 的 tools.custom",
        )
        extension_names = custom_tool_names | self._resolved_mcp_tool_names(tool_config)
        development = config.get("development") or {}
        universe = build_agent_tool_universe(
            extension_names=extension_names,
            include_test_tools=development.get("test_tools", False),
        )
        return resolve_tool_policy(
            universe_names=universe,
            extension_names=extension_names,
            allowlist=tool_config["allowlist"],
            denylist=tool_config["denylist"],
            context=f"agent {resolved_agent_id} 的工具策略",
        )

    def resolve_agent_confirmation_tool_names(
        self,
        agent_id: str | None = None,
    ) -> frozenset[str]:
        config = self._get_effective_config()
        resolved_agent_id = self._normalize_agent_id(agent_id)
        tool_config = self._agent_tool_config_from_loaded(
            config,
            agent_id=resolved_agent_id,
        )
        custom_tool_names = custom_tool_spec_names(
            tool_config["custom"],
            context=f"agent {resolved_agent_id} 的 tools.custom",
        )
        extension_names = custom_tool_names | self._resolved_mcp_tool_names(tool_config)
        development = config.get("development") or {}
        universe = build_agent_tool_universe(
            extension_names=extension_names,
            include_test_tools=development.get("test_tools", False),
        )
        return resolve_tool_selectors(
            selectors=tool_config["confirmation_required"],
            universe_names=universe,
            extension_names=extension_names,
            context=f"agent {resolved_agent_id} 的 tools.confirmation_required",
        )

    def _agent_tool_config_from_loaded(
        self,
        config: dict[str, Any],
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        agents = config.get("agents", {})
        if not agents or agent_id not in agents:
            if agent_id != "default":
                raise ValueError(f"agent {agent_id} 不存在")
            return {
                "allowlist": [],
                "denylist": [],
                "confirmation_required": [],
                "custom": [],
            }
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        agent_config = agents[agent_id]
        if not isinstance(agent_config, dict):
            raise ValueError(f"agent {agent_id} 的配置必须是对象")
        return self._parse_agent_tool_config(
            agent_config.get("tools", {}),
            agent_id=agent_id,
        )

    def _validate_agent_tool_policies(
        self,
        config: dict[str, Any],
        *,
        mcp_tool_names: frozenset[str] | None = None,
    ) -> None:
        agents = config.get("agents", {})
        if agents is None:
            return
        if not isinstance(agents, dict):
            raise ValueError("agents 配置必须是对象")
        development = config.get("development", {})
        if development is None:
            development = {}
        if not isinstance(development, dict):
            raise ValueError("development 配置必须是对象")
        include_test_tools = development.get("test_tools", False)
        if not isinstance(include_test_tools, bool):
            raise ValueError("development.test_tools 必须是布尔值")

        for agent_id, agent_config in agents.items():
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("agents 的键必须是非空字符串")
            if not isinstance(agent_config, dict):
                raise ValueError(f"agent {agent_id} 的配置必须是对象")
            tool_config = self._parse_agent_tool_config(
                agent_config.get("tools", {}),
                agent_id=agent_id,
            )
            raw_tools_config = agent_config.get("tools")
            if isinstance(raw_tools_config, dict) and "custom" in raw_tools_config:
                # ConfigService 返回的配置即为运行时权威配置，因此在校验入口
                # 写回共享解析器产生的 strip/类型归一化结果，避免 schema、
                # 工具目录和 factory 分别观察到不同的扩展工具声明。
                raw_tools_config["custom"] = tool_config["custom"]
            custom_tool_names = custom_tool_spec_names(
                tool_config["custom"],
                context=f"agent {agent_id} 的 tools.custom",
            )
            extension_names = custom_tool_names | self._resolved_mcp_tool_names(
                tool_config,
                mcp_tool_names=mcp_tool_names,
            )
            universe = build_agent_tool_universe(
                extension_names=extension_names,
                include_test_tools=include_test_tools,
            )
            resolve_tool_policy(
                universe_names=universe,
                extension_names=extension_names,
                allowlist=tool_config["allowlist"],
                denylist=tool_config["denylist"],
                context=f"agent {agent_id} 的工具策略",
            )
            resolve_tool_selectors(
                selectors=tool_config["confirmation_required"],
                universe_names=universe,
                extension_names=extension_names,
                context=f"agent {agent_id} 的 tools.confirmation_required",
            )

    @staticmethod
    def _preflight_custom_tool_factories(
        config: dict[str, Any],
        *,
        source_path: Path,
    ) -> None:
        agents = config.get("agents")
        if agents is None:
            return
        if not isinstance(agents, dict):
            raise ValueError(f"配置源 agents 必须是对象: {source_path}")
        for agent_id, agent_config in agents.items():
            if not isinstance(agent_config, dict):
                continue
            tools = agent_config.get("tools")
            if not isinstance(tools, dict) or "custom" not in tools:
                continue
            raw_custom = tools["custom"]
            if not isinstance(raw_custom, list):
                raise ValueError(
                    f"配置源 {source_path} 的 agents.{agent_id}.tools.custom 必须是数组"
                )
            specs = parse_custom_tool_specs(
                raw_custom,
                context=(f"配置源 {source_path} 的 agents.{agent_id}.tools.custom"),
            )
            for spec in specs:
                try:
                    load_custom_tool_factory(spec.factory_path)
                except (ImportError, AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "配置源扩展工具预检失败: "
                        f"path={source_path}, agent={agent_id}, "
                        f"tool={spec.name}, factory={spec.factory_path}"
                    ) from exc

    def _resolved_mcp_tool_names(
        self,
        tool_config: dict[str, Any],
        *,
        mcp_tool_names: frozenset[str] | None = None,
    ) -> frozenset[str]:
        if mcp_tool_names is not None:
            return mcp_tool_names
        if self._mcp_tool_names is not None:
            return self._mcp_tool_names
        referenced_names = {
            name
            for field_name in ("allowlist", "denylist", "confirmation_required")
            for name in tool_config[field_name]
            if isinstance(name, str) and name.startswith("mcp__")
        }
        return frozenset(referenced_names)

    @staticmethod
    def _parse_agent_tool_config(
        raw_tools_config: object,
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        if raw_tools_config is None:
            raw_tools_config = {}
        if not isinstance(raw_tools_config, dict):
            raise ValueError(f"agent {agent_id} 的 tools 配置必须是对象")

        result: dict[str, Any] = {}
        for field_name in ("allowlist", "denylist", "confirmation_required"):
            value = raw_tools_config.get(field_name, [])
            if not isinstance(value, list):
                raise ValueError(f"agent {agent_id} 的 tools.{field_name} 必须是数组")
            result[field_name] = list(value)
        raw_custom = raw_tools_config.get("custom", [])
        if not isinstance(raw_custom, list):
            raise ValueError(f"agent {agent_id} 的 tools.custom 必须是数组")
        result["custom"] = [
            spec.to_config()
            for spec in parse_custom_tool_specs(
                raw_custom,
                context=f"agent {agent_id} 的 tools.custom",
            )
        ]
        return result
