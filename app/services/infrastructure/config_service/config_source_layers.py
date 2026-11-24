"""ConfigSourceLayersMixin：ConfigService 的 source-layer 读取与来源权威表方法族（纯搬迁）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config_migration_guard import require_settled_config_migration
from app.core.config_sources import (
    ConfigSource,
    ConfigSourceLayer,
    StableConfigFile,
    config_revision,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.core.path_utils import (
    get_user_workspace_config_path,
    get_user_workspace_local_config_path,
    get_user_workspace_schema_path,
    get_workspace_config_path,
)
from app.core.user_identity import user_scope_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.runtime.network_proxy import capture_proxy_environment
from app.schemas.internal_v2.config import ConfigSourceBaselineDTO
from app.services.infrastructure.config import (
    SHARED_USER_WORKSPACE_SOURCE_KEY,
)
from app.services.infrastructure.config.source_uri import (
    config_file_uri,
    inline_config_source_uri,
)
from app.services.infrastructure.config.state import (
    prepare_config_for_persistence,
)
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_entries,
)
from app.services.infrastructure.config_service.config_agent_tools import (
    ConfigAgentToolsMixin,
)
from app.services.infrastructure.config_service.config_service_common import (
    _INLINE_SOURCE_KEY,
    release_inline_config_uri_for_file,
)
from configs.inheritance import ConfigGraph
from configs.inheritance.values import ConfigValue, json_pointer, thaw
from configs.runtime import (
    read_jsonc_object,
)

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService


class ConfigSourceLayersMixin:
    if TYPE_CHECKING:
        _RUNTIME_OVERRIDE_CONFIG_KEY: str

    def _resolve_schema_path(
        self,
        *,
        source_configs: Mapping[Path, Mapping[str, object]] | None = None,
    ) -> Path:
        config_path = self._get_workspace_config_path()
        candidates = [self._inline_config_path]
        if self._workspace_state_store is None:
            candidates.insert(0, config_path)
        for path in candidates:
            if not path.is_file():
                continue
            # 候选构建复用本次已解析的原始来源，不从合并后的 $schema 推断归属。
            config = source_configs.get(path) if source_configs is not None else None
            if config is None:
                config = read_jsonc_object(path)
            schema_reference = config.get("$schema")
            if (
                isinstance(schema_reference, str)
                and schema_reference
                and "://" not in schema_reference
            ):
                referenced_path = (path.parent / schema_reference).resolve()
                if referenced_path.is_file():
                    return referenced_path
        configured_schema = self._config_dir / "workspace_schema.jsonc"
        if configured_schema.is_file():
            return configured_schema
        installed_schema = get_user_workspace_schema_path()
        if installed_schema.is_file():
            return installed_schema
        error = FileNotFoundError(
            "Workspace 配置 schema 不存在: "
            f"config={config_path} expected={configured_schema}"
        )
        raise FileNotFoundError(
            "Workspace 配置 schema 不存在: 工作区引用、发行包引用、配置目录及用户安装目录均无可用文件"
        ) from error

    def _load_schema(self, schema_path: Path | None = None) -> dict[str, Any]:
        if self._schema is None:
            self._schema = read_jsonc_object(
                schema_path if schema_path is not None else self._resolve_schema_path()
            )
        return self._schema

    def _get_workspace_config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        return get_user_workspace_config_path()

    def _source_locations(self) -> dict[Path, tuple[str, ConfigSourceLayer]]:
        paths = {
            self._inline_config_path.resolve(): (_INLINE_SOURCE_KEY, "inline"),
            self._get_workspace_config_path().resolve(): ("workspace_mutable_override", "user"),
            self._get_workspace_local_config_path().resolve(): ("workspace_local_mutable_override", "user_local"),
        }
        if self._workspace_root is not None:
            paths[get_workspace_config_path(self._workspace_root).resolve()] = ("workspace_root_mutable_override", "workspace")
        return paths

    def _source_identity(self, path: Path) -> tuple[str, ConfigSourceLayer]:
        return self._source_locations().get(path, ("source_" + sha256(str(path).encode()).hexdigest(), "source"))

    def _source_uri_for_path(self, path: Path) -> str:
        if path == self._inline_config_path.resolve():
            return self._inline_source_uri()
        key, layer = self._source_identity(path)
        workspace_id = self._source_owner_workspace_id
        if layer == "workspace" or self._workspace_root is not None and path.is_relative_to(self._workspace_root):
            if self._workspace_root is None:
                raise ValueError("Workspace 来源缺少工作区归属")
            return config_file_uri(scope="workspace", scope_id=workspace_id or load_or_create_workspace_id(self._workspace_root), logical_name=key)
        return config_file_uri(scope="user", scope_id=user_scope_id(), logical_name=key)

    def _build_source_graph(
        self, *, source_documents=None, document_overrides=None, read_source=read_stable_config_file,
        runtime_override: dict[str, object] | None = None,
    ) -> ConfigGraph:
        schema = self._load_schema()
        entry = (get_workspace_config_path(self._workspace_root) if self._workspace_root is not None
                 else self._get_workspace_local_config_path())
        known = self._source_locations()
        overrides = self._runtime_config_overrides if runtime_override is None else runtime_override
        return ConfigGraph(
            domain="workspace", version=4,
            owner_id=self._source_owner_workspace_id or (load_or_create_workspace_id(self._workspace_root)
                if self._workspace_root is not None else user_scope_id()),
            entry=entry, aliases={"builtin:workspace": self._inline_config_path,
                "user:workspace": self._get_workspace_config_path(),
                "user:workspace_local": self._get_workspace_local_config_path()},
            settings_schema={"$ref": "#/$defs/effectiveSettings", "$defs": schema["$defs"]},
            options_schema={"$ref": "#/$defs/effectiveOptions", "$defs": schema["$defs"]},
            source_uri=self._source_uri_for_path, source_key=lambda path: self._source_identity(path)[0],
            authorize_source=lambda path: ".boxteam" not in path.parts or self._workspace_root is not None
                and path.is_relative_to(self._workspace_root),
            registered_uris={self._source_uri_for_path(path): path for path in known},
            read_source=read_source, source_documents=source_documents, document_overrides=document_overrides,
            scope_settings_override={"workspace": {"ui": overrides}} if overrides else None,
            proxy_environment=capture_proxy_environment(),
            document_schema=schema,
        )

    def _read_effective_config(
        self, *, source_override: tuple[str, dict[str, object]] | None = None,
        import_sources: bool = True, source_payloads: dict[str, dict[str, object]] | None = None,
        source_files: dict[str, StableConfigFile] | None = None, operation_only: bool = False,
        frozen_files: Mapping[Path, StableConfigFile] | None = None,
    ) -> tuple[dict[str, Any], tuple[Path, ...], tuple[ConfigSource, ...], Path, ConfigGraph]:
        """唯一显式父链；来源物化仍由原 Workspace store 和 fan-out owner 持有。"""
        def read_source(path: Path) -> StableConfigFile:
            require_settled_config_migration(path)
            if frozen_files is not None and path in frozen_files:
                return frozen_files[path]
            return read_stable_config_file(path)

        paths = {key: path for path, (key, _) in self._source_locations().items()}
        current = getattr(self, "_candidate_source_graph", None)
        if current is not None:
            paths.update({current.source_keys[uri]: path for uri, path in current.source_locations.items()})
        overrides = ({self._source_uri_for_path(paths[source_override[0]]): source_override[1]}
                     if source_override is not None else None)
        graph = self._build_source_graph(document_overrides=overrides, read_source=read_source)
        self._candidate_source_graph = graph
        try:
            scope = graph.resolve_scope()
            workspace = cast(Mapping[str, ConfigValue], scope.settings["workspace"])
            # 所有已配置消费者在发布前解析，暖读与历史恢复不延迟读取新文件。
            for provider_id, provider in cast(Mapping, workspace["llm"]["providers"]).items():
                graph.resolve_provider(provider_id)
                for model_id in provider["models"]:
                    graph.resolve_model(provider_id, model_id)
        except Exception as error:
            if import_sources:
                self._record_failed_source_read(graph, error)
            raise
        ordered = list(dict.fromkeys([node.location.path for node in scope.chain] + list(graph.dependencies)))
        details = []
        for precedence, path in enumerate(ordered):
            uri = self._source_uri_for_path(path)
            key, layer = self._source_identity(path)
            file = graph.dependencies[path]
            document = cast(dict[str, object], thaw(graph.source_documents[uri]))
            business = cast(dict[str, object], document["settings"]).get("workspace", {})
            if layer in {"user", "user_local"} and workspace_plugin_entries(business):
                raise ValueError("plugins.runtime.entries 只能写入明确工作区的配置来源")
            if not operation_only:
                prepare_config_for_persistence(document, resolve_environment=False)
                ConfigAgentToolsMixin._preflight_custom_tool_factories(business)
            record = self._workspace_state_store.get_source_layer(key) if self._workspace_state_store is not None else None
            if import_sources and self._workspace_state_store is not None and layer != "inline":
                verify_stable_config_file(file)
                previous_generation = record.source_generation if record is not None else 0
                record = self._workspace_state_store.sync_config_source(
                    config_key=key, uri=uri, config_version=4, presence="present", payload=document,
                    layer_digest=file.digest, expected_layer_revision=record.layer_revision if record else None,
                    expected_layer_digest=record.layer_digest if record else None,
                    journal_origin=None if self._shared_source_key(key) is not None else "file-watcher",
                )
                self._record_source_journal(record, origin="file-watcher", previous_source_generation=previous_generation)
                record = self._workspace_state_store.get_source_layer(key)
            details.append(ConfigSource(uri=uri, layer=layer, precedence=precedence, loaded=True,
                source_key=key, presence="present", layer_revision=record.layer_revision if record else None,
                layer_digest=record.layer_digest if record else file.digest,
                source_generation=record.source_generation if record else None))
            if source_payloads is not None:
                source_payloads[key] = document
            if source_files is not None:
                source_files[key] = file
        if self._runtime_config_overrides:
            details.append(replace(self._runtime_override_source(), precedence=len(details)))
            if source_payloads is not None:
                source_payloads[self._RUNTIME_OVERRIDE_CONFIG_KEY] = {"settings": {"workspace": {"ui": dict(self._runtime_config_overrides)}}}
        settings = cast(dict[str, object], thaw(scope.settings))
        config = {**settings["workspace"], "network": settings["network"], "config_version": 4}
        if not operation_only:
            self._validate_agent_tool_policies(config)
        return config, tuple(ordered), tuple(details), self._resolve_schema_path(), graph

    @staticmethod
    def _graph_origins(graph: ConfigGraph) -> list[dict[str, object]]:
        scope = graph.resolve_scope()
        result = []

        def visit(value: ConfigValue, path: tuple[str, ...]) -> None:
            if isinstance(value, Mapping) and value:
                for key, child in value.items():
                    visit(child, (*path, key))
                return
            origin = scope.origins.locate(path)
            key = graph.source_keys[origin.source_uri]
            shadowed = []
            for node in scope.chain:
                try:
                    parent_key = graph.source_keys[node.origins.locate(path).source_uri]
                except KeyError:
                    continue
                if parent_key != key and parent_key not in shadowed:
                    shadowed.append(parent_key)
            result.append({"path": json_pointer(path[1:] if path[0] == "workspace" else path),
                "source_key": key, "shadowed_sources": shadowed})

        for namespace in ("workspace", "network"):
            visit(scope.settings[namespace], (namespace,))
        return result

    def _read_saved_source_view(self) -> tuple[
        dict[str, Any], dict[str, dict[str, object]],
        dict[str, StableConfigFile], dict[str, ConfigSourceBaselineDTO],
    ]:
        """读取本次保存态及原来源版本；不导入来源、发布候选或执行运行时审计。"""
        service = cast("ConfigService", self)
        layers: dict[str, dict[str, object]] = {}
        files: dict[str, StableConfigFile] = {}
        config, _, sources, _, _ = service._read_effective_config(
            import_sources=False, source_payloads=layers, source_files=files, operation_only=True,
        )
        baseline = {}
        for source in sources:
            assert source.source_key is not None
            file = files.get(source.source_key)
            baseline[source.source_key] = ConfigSourceBaselineDTO(
                uri=source.uri, presence=source.presence,
                layer_revision=source.layer_revision, layer_digest=source.layer_digest,
                source_generation=source.source_generation,
                file_presence=file.presence if file is not None else None,
                file_digest=file.digest if file is not None else None,
            )
        return config, layers, files, baseline

    def _runtime_override_source(self) -> ConfigSource:
        # runtime_override 是逻辑来源；物理 carrier 不属于来源身份，且没有来源文件，
        # 故 uri=None。
        layer, precedence = "runtime_override", 0
        if self._workspace_state_store is None:
            return ConfigSource(
                uri=None,
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
            uri=None,
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
            uri=None,
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
            uri=record.uri,
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

    def _inline_source_uri(self) -> str:
        """构造发行包内 inline 层配置来源的 URI（唯一可寻址的 config 层）。"""
        return inline_config_source_uri(logical_name=self._inline_logical_name)

    def get_schema_source_uri(self) -> str | None:
        """仅当生效 schema 就是发行包 inline schema 时返回其 URI，否则返回 None。

        `_resolve_schema_path()` 有四个返回分支：工作区 `$schema` 相对引用解析出的
        文件、发行包内 `*_inline.jsonc` 的 `$schema` 引用、`config_dir` 下的 schema、
        用户级安装 schema。安装链路把发行包 schema **原字节拷贝**到用户配置目录（
        `configs/installer.py` 的 `atomic_write(schema_target, schema_source.read_bytes())`），
        故「真属发行包 inline 层」的正确判据是**与发行包 schema 内容一致**，而非路径相等
        ——路径相等会把正常安装（用户级拷贝）误判成非 inline。

        用户自定义 `$schema`（内容与发行包不同）即判为不可寻址，返回 None。返回 None 由
        调用方以 null 对外表达；MUST NOT 在此抛错，故含点号或非法字符的自定义 stem
        永不进入 URI 构造。URI 尾段取发行包 schema 的逻辑资源名，MUST NOT 取用户文件 stem。
        """
        try:
            effective_schema = self._resolve_schema_path()
        except FileNotFoundError:
            return None
        return release_inline_config_uri_for_file(
            effective_schema,
            release_config_name="workspace_schema.jsonc",
        )

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
        shared_key = self._shared_source_key(source_key)
        if shared_key is not None:
            source_owner = self._source_owner
            workspace_id = self._source_owner_workspace_id
            if source_owner is None or workspace_id is None:
                raise RuntimeError("共享 Workspace source 缺少 source owner 身份")
            owner_record = source_owner.observe(
                uri=source_record.uri,
                presence=source_record.presence,
                layer_digest=source_record.layer_digest,
                origin=origin,
                source_key=shared_key,
            )
            materialized = self._workspace_state_store.update_source_generation(
                config_key=source_key,
                source_generation=owner_record.source_generation,
                expected_layer_revision=source_record.layer_revision,
                expected_layer_digest=source_record.layer_digest,
            )
            source_owner.prepare_fanout(
                workspace_id=workspace_id,
                source_key=shared_key,
                after_generation=previous_source_generation,
            )
            for prior in source_owner.list_journal(
                source_key=shared_key,
                after_generation=previous_source_generation,
            ):
                is_current = prior.source_generation == owner_record.source_generation
                source_owner.record_fanout(
                    source_key=shared_key,
                    source_generation=prior.source_generation,
                    workspace_id=workspace_id,
                    status="applying" if is_current else "superseded",
                    layer_revision=(materialized.layer_revision if is_current else None),
                    layer_digest=(materialized.layer_digest if is_current else None),
                    result="materialized" if is_current else "superseded",
                )
            if owner_record.source_generation <= previous_source_generation:
                observed = next(
                    (item for item in source_owner.list_fanout(
                        source_generation=owner_record.source_generation, source_key=shared_key,
                    ) if item.workspace_id == workspace_id),
                    None,
                )
                if observed is None or observed.status in {"pending", "unobserved"}:
                    source_owner.record_fanout(
                        source_key=shared_key,
                        source_generation=owner_record.source_generation,
                        workspace_id=workspace_id,
                        status="applying",
                        layer_revision=materialized.layer_revision,
                        layer_digest=materialized.layer_digest,
                        result="materialized",
                    )
            return
        self._workspace_state_store.append_config_source_journal(
            source_key=source_key,
            source_event_id=f"{source_key}:layer:{source_record.layer_revision}",
            uri=source_record.uri,
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

    def _record_shared_source_application(
        self,
        *,
        sources: tuple[ConfigSource, ...],
        result: Literal["applied", "unchanged", "restart_required", "conflict", "failed"],
        error: str | None = None,
    ) -> None:
        """层物化只表示 applying；原候选链结清后才记录实际应用结果。"""
        service = cast("ConfigService", self)
        owner, workspace_id = service._source_owner, service._source_owner_workspace_id
        if owner is None or workspace_id is None:
            return
        for source in sources:
            shared_key = self._shared_source_key(source.source_key)
            if shared_key is None or not source.source_generation:
                continue
            record = owner.get_fanout(source_key=shared_key,
                source_generation=source.source_generation, workspace_id=workspace_id)
            if record.status not in {"pending", "applying"}:
                continue
            if record.result == "restart_required" and result == "restart_required":
                continue
            status = {"applied": "applied", "unchanged": "applied", "restart_required": "applying",
                      "conflict": "conflict", "failed": "failed"}[result]
            owner.record_fanout(source_key=shared_key,
                source_generation=source.source_generation, workspace_id=workspace_id, status=status,
                layer_revision=source.layer_revision, layer_digest=source.layer_digest,
                result=result, error=error)

    def _record_failed_source_read(self, graph: ConfigGraph, error: Exception) -> None:
        """无效文档不物化为可用配置，但实际共享来源的失败必须可查询。"""
        owner, workspace_id = self._source_owner, self._source_owner_workspace_id
        if owner is None or workspace_id is None:
            return
        for path, file in graph.dependencies.items():
            shared_key = self._shared_source_key(self._source_identity(path)[0])
            if shared_key is None:
                continue
            observed = owner.observe(source_key=shared_key, uri=self._source_uri_for_path(path),
                presence=file.presence, layer_digest=file.digest, origin="file-watcher")
            owner.record_fanout(source_key=shared_key, source_generation=observed.source_generation,
                workspace_id=workspace_id, status="failed", layer_digest=file.digest,
                result="failed", error=f"Workspace 配置来源读取失败: error={type(error).__name__}")

    def _shared_source_key(self, source_key: str | None) -> str | None:
        """沿原来源账本按实际文件身份汇总；工作区自行观察并提交，不由 Gateway 驱动。"""
        if self._source_owner is None or self._source_owner_workspace_id is None:
            return None
        if source_key == "workspace_mutable_override":
            return SHARED_USER_WORKSPACE_SOURCE_KEY
        if source_key == "workspace_local_mutable_override":
            return "workspace:user_local"
        if source_key is not None and source_key.startswith("source_"):
            return "workspace:" + source_key
        return None

    def _get_workspace_local_config_path(self) -> Path:
        if self._config_path is None:
            return get_user_workspace_local_config_path()
        return self._get_workspace_config_path().parent / "workspace_local.jsonc"


__all__ = ["ConfigSourceLayersMixin"]
