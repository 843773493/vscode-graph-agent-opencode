"""ConfigSourceLayersMixin：ConfigService 的 source-layer 读取与来源权威表方法族（纯搬迁）。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.core.config_sources import (
    ConfigSource,
    config_revision,
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.core.path_utils import (
    get_user_workspace_config_path,
    get_user_workspace_local_config_path,
    get_user_workspace_schema_path,
    get_workspace_config_path,
)
from app.services.infrastructure.config import (
    SHARED_USER_WORKSPACE_SOURCE_KEY,
)
from app.services.infrastructure.config.source_vrn import inline_config_source_vrn
from app.services.infrastructure.config.state import (
    SecretReferenceRequiredError,
    dump_json,
    prepare_config_for_persistence,
    redact_config_payload,
    restore_environment_secret_references,
)
from app.services.infrastructure.config_service.config_agent_tools import (
    ConfigAgentToolsMixin,
)
from app.services.infrastructure.config_service.config_service_common import (
    _SOURCE_LAYER_AUTHORITY,
    release_inline_config_vrn_for_file,
)
from configs.layout_migrations import migrate_legacy_workspace_configuration
from configs.runtime import merge_json_objects, read_jsonc_object


class ConfigSourceLayersMixin:
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
                vrn=self._inline_source_vrn(),
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
        # runtime_override 是逻辑来源；物理 carrier 不属于来源身份，且没有来源文件，
        # 故 vrn=None。
        layer, precedence = _SOURCE_LAYER_AUTHORITY[self._RUNTIME_OVERRIDE_CONFIG_KEY]
        if self._workspace_state_store is None:
            return ConfigSource(
                vrn=None,
                layer=layer,
                precedence=precedence,
                loaded=bool(self._runtime_config_overrides),
                source_key=self._RUNTIME_OVERRIDE_CONFIG_KEY,
                presence=("present" if self._runtime_config_overrides else "absent"),
            )
        source_record = self._workspace_state_store.get_source_layer(
            self._RUNTIME_OVERRIDE_CONFIG_KEY
        )
        return ConfigSource(
            vrn=None,
            layer=layer,
            precedence=precedence,
            loaded=bool(self._runtime_config_overrides),
            source_key=self._RUNTIME_OVERRIDE_CONFIG_KEY,
            presence=(
                source_record.presence if source_record is not None else "absent"
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
            vrn=None,
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
            vrn=record.vrn,
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

    def _inline_source_vrn(self) -> str:
        """构造发行包内 inline 层配置来源的 VRN（唯一可寻址的 config 层）。"""
        return inline_config_source_vrn(logical_name=self._inline_logical_name)

    def get_schema_source_vrn(self) -> str | None:
        """仅当生效 schema 就是发行包 inline schema 时返回其 VRN，否则返回 None。

        `_resolve_schema_path()` 有四个返回分支：工作区 `$schema` 相对引用解析出的
        文件、发行包内 `*_inline.jsonc` 的 `$schema` 引用、`config_dir` 下的 schema、
        用户级安装 schema。安装链路把发行包 schema **原字节拷贝**到用户配置目录（
        `configs/installer.py` 的 `atomic_write(schema_target, schema_source.read_bytes())`），
        故「真属发行包 inline 层」的正确判据是**与发行包 schema 内容一致**，而非路径相等
        ——路径相等会把正常安装（用户级拷贝）误判成非 inline。

        用户自定义 `$schema`（内容与发行包不同）即判为不可寻址，返回 None。返回 None 由
        调用方以 null 对外表达；MUST NOT 在此抛错，故含点号或非法字符的自定义 stem
        永不进入 VRN 构造。VRN 尾段取发行包 schema 的逻辑资源名，MUST NOT 取用户文件 stem。
        """
        try:
            effective_schema = self._resolve_schema_path()
        except FileNotFoundError:
            return None
        return release_inline_config_vrn_for_file(
            effective_schema,
            release_config_name="workspace_schema.jsonc",
        )

    def _config_source(
        self,
        *,
        path: Path,
        config_key: str,
    ) -> ConfigSource:
        """返回一条来源的 VRN 兄弟字段形态；inline 之外的层不可寻址。

        逻辑来源层与 precedence 一律取自 `_SOURCE_LAYER_AUTHORITY`，本方法 MUST NOT
        再接收调用方传入的层名：从源 JSONC 构建只影响 `loaded`/`presence`（读 `path`），
        绝不改变对外 `layer`。有 state store 时 user/user_local/workspace 三层共享同一
        个 `workspace.sqlite`，但共享只是**承载事实**，MUST NOT 有损改写成 `sqlite`
        对外暴露——它们在权威轴上本就是三个不同层。不可寻址性以 `vrn=None` 表达。
        real path 只在本调用栈内用于判断 presence，MUST NOT 持久化或对外。
        """
        layer, resolved_precedence = _SOURCE_LAYER_AUTHORITY[config_key]
        if self._workspace_state_store is None:
            return ConfigSource(
                vrn=None,
                layer=layer,
                precedence=resolved_precedence,
                loaded=path.is_file(),
                source_key=config_key,
                presence="present" if path.is_file() else "absent",
            )
        source_record = self._workspace_state_store.get_source_layer(config_key)
        return ConfigSource(
            vrn=None,
            layer=layer,
            precedence=resolved_precedence,
            loaded=(
                source_record.presence == "present"
                if source_record is not None
                else path.is_file()
            ),
            presence=(
                source_record.presence
                if source_record is not None
                else ("present" if path.is_file() else "absent")
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
        return path.is_file()

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
            if source_record is None:
                return None
            deleted_backup_path = path.with_name(f"{path.name}.deleted.bak")
            if (
                not deleted_backup_path.exists()
                and source_record.payload is not None
            ):
                deleted_backup_path.write_text(
                    dump_json(redact_config_payload(source_record.payload)),
                    encoding="utf-8",
                )
            verify_stable_config_file(file_snapshot)
            source_record = self._workspace_state_store.sync_config_source(
                config_key=config_key,
                # user/user_local/workspace 层均不可寻址，依据是共享同一 workspace.sqlite
                # 这一边界载体（非 scope 闭集），故 vrn 恒为 None。
                vrn=None,
                config_version=source_record.config_version,
                presence="absent",
                payload=None,
                layer_digest=None,
                expected_layer_revision=(
                    source_record.layer_revision if source_record is not None else None
                ),
                expected_layer_digest=(
                    source_record.layer_digest if source_record is not None else None
                ),
                journal_origin=journal_origin,
            )
            self._record_source_journal(
                source_record,
                source_path=path,
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
            # 该层不可寻址：持久化的 vrn 必为 None，与读取点的 None 一致才走复用分支。
            and source_record.vrn is None
        ):
            payload = parse_stable_config_file(file_snapshot)
            if payload is None:
                raise RuntimeError(f"present 配置文件解析为空: {path}")
            prepare_config_for_persistence(payload)
            self._preflight_override(payload, source_path=self._workspace_state_store.path)
            verify_stable_config_file(file_snapshot)
            source_record = self._workspace_state_store.sync_config_source(
                config_key=config_key,
                vrn=None,
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
                source_path=path,
                origin="loader",
                previous_source_generation=previous_source_generation,
            )
            return dict(payload)

        try:
            payload = parse_stable_config_file(file_snapshot)
        except Exception:
            if (
                source_record is not None
                and source_record.payload is not None
                and not self._snapshot_store.has_snapshot()
            ):
                restored = restore_environment_secret_references(source_record.payload)
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
            vrn=None,
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
            journal_origin=journal_origin,
        )
        self._record_source_journal(
            source_record,
            source_path=path,
            origin="file-watcher",
            previous_source_generation=previous_source_generation,
        )
        return payload

    def _record_source_journal(
        self,
        source_record,
        *,
        source_path: Path,
        origin: str,
        previous_source_generation: int = 0,
    ) -> None:
        # ``source_path`` 是最后访问点的真实路径，只在本调用栈内用于判定共享来源，
        # MUST NOT 持久化（journal 只写 VRN 兄弟字段）。
        if self._workspace_state_store is None:
            return
        source_key = source_record.config_key
        if self._is_shared_user_source(
            config_key=source_key,
            path=source_path,
        ):
            source_owner = self._source_owner
            workspace_id = self._source_owner_workspace_id
            if source_owner is None or workspace_id is None:
                raise RuntimeError("共享 Workspace source 缺少 source owner 身份")
            owner_record = source_owner.observe(
                vrn=source_record.vrn,
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
            vrn=source_record.vrn,
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
    def _read_config_source(
        path: Path,
    ) -> dict[str, Any]:
        config_snapshot = read_stable_config_file(path)
        config = parse_stable_config_file(config_snapshot)
        if config is None:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        ConfigAgentToolsMixin._preflight_custom_tool_factories(config, source_path=path)
        return config

    @staticmethod
    def _preflight_override(payload: dict[str, Any], *, source_path: Path) -> None:
        # SQLite 中的数据已经经过 JSON 解析；仍执行自定义工具工厂预检，保持来源边界一致。
        ConfigAgentToolsMixin._preflight_custom_tool_factories(payload, source_path=source_path)

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


__all__ = ["ConfigSourceLayersMixin"]
