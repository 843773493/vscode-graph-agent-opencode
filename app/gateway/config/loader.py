from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

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
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.plugins.sources import plugin_sources
from app.services.infrastructure.config.source_uri import (
    config_file_uri,
    inline_config_source_uri,
)
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    SecretReferenceRequiredError,
)
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    validate_skill_sources,
)
from configs.inheritance import ConfigGraph
from configs.inheritance.values import thaw
from configs.installer import resolve_config_resource_source
from configs.network import NetworkSettings
from configs.plugin_parameters import bound_plugin_configuration
from configs.runtime import (
    read_jsonc_object,
    validate_config_payload,
)

from .connection_ids import _normalize_connection_ids
from .sources import _gateway_persisted_source_details
from .values import (
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
    GatewayConfig,
    _history_loading_config,
    _nested_config_value,
    _positive_integer_config,
    _positive_number_config,
    _skill_groups_config,
    _workspace_from_validated_config,
)


def load_gateway_config(
    *,
    config_path: Path | None = None,
    schema_path: Path | None = None,
    local_config_path: Path | None = None,
    inline_config_path: Path | None = None,
    state_store: GatewayStateStore | None = None,
    persist_migrations: bool = True,
    startup: bool = False,
    gateway_id: str | None = None,
    source_overrides: dict[str, dict[str, object]] | None = None,
    source_payloads: dict[str, dict[str, object]] | None = None,
    allow_new_connection_ids: bool = True,
    entry_path: Path | None = None,
    dependencies: dict[Path, StableConfigFile] | None = None,
) -> GatewayConfig:
    """从明确入口解析唯一父链；业务消费者继续读取本域平面投影。"""
    resolved_config_path = config_path or get_user_gateway_config_path()
    resolved_schema_path = schema_path or get_user_gateway_schema_path()
    schema = read_jsonc_object(resolved_schema_path)
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not {"effectiveSettings", "gatewayPayload"} <= definitions.keys():
        raise ValueError("Gateway schema 缺少 v6 源节点与业务投影定义")
    resolved_inline_config_path = inline_config_path or resolve_config_resource_source(
        "gateway_inline.jsonc"
    )
    resolved_local_config_path = local_config_path or (
        get_user_gateway_local_config_path()
        if config_path is None
        else resolved_config_path.parent / "gateway_local.jsonc"
    )
    entry = (entry_path or (resolved_local_config_path if config_path is None else resolved_config_path)).resolve()
    known = {
        resolved_inline_config_path.resolve(): ("inline:0", "inline"),
        resolved_config_path.resolve(): ("gateway_mutable_override", "user"),
        resolved_local_config_path.resolve(): ("gateway_local_mutable_override", "user_local"),
    }

    def source_identity(path: Path) -> tuple[str, ConfigSourceLayer]:
        return known.get(path, ("source_" + sha256(str(path).encode()).hexdigest(), "source"))

    def source_uri(path: Path) -> str:
        if path == resolved_inline_config_path.resolve():
            return inline_config_source_uri(logical_name="gateway_inline")
        key, _ = source_identity(path)
        return config_file_uri(scope="gateway" if gateway_id else "user", scope_id=gateway_id or "local", logical_name=key)

    def read_source(path: Path) -> StableConfigFile:
        require_settled_config_migration(path)
        file = read_stable_config_file(path)
        if dependencies is not None:
            dependencies[path] = file
        return file

    source_paths: list[Path] = []
    source_details: list[ConfigSource] = []
    loaded_persisted_snapshot = False
    persisted_source_baseline: dict[str, object] | None = None
    documents = None
    if startup and state_store is not None:
        blocked = state_store.migrate_legacy_active_snapshot_secrets(
            config_domain="gateway"
        )
        if blocked:
            raise SecretReferenceRequiredError(
                "旧 Gateway active snapshot 含无法恢复的秘密摘要，必须重新导入: "
                + ", ".join(blocked)
            )
        candidate_ref = os.environ.get("BOXTEAM_CONFIG_CANDIDATE_REF")
        persisted_payload: dict[str, object] | None = None
        if candidate_ref:
            pending = state_store.load_gateway_pending_candidate(
                candidate_ref=candidate_ref,
                gateway_id=gateway_id,
            )
            generation = os.environ.get("BOXTEAM_CONFIG_GENERATION")
            fencing_token = os.environ.get("BOXTEAM_CONFIG_FENCING_TOKEN")
            if not generation or not fencing_token:
                raise ConfigConflictError(
                    "Gateway pending 启动缺少 generation 或 fencing token"
                )
            if pending.target_generation != generation:
                raise ConfigConflictError(
                    "Gateway pending 启动 generation 不匹配: "
                    f"candidate={pending.target_generation}, process={generation}"
                )
            if pending.fencing_token != fencing_token:
                raise ConfigConflictError("Gateway pending 启动 fencing token 不匹配")
            persisted_payload = pending.payload
            persisted_source_baseline = pending.source_baseline
        else:
            active = state_store.get_active_config_snapshot("gateway")
            if active is not None:
                if active.state != "active":
                    raise ConfigConflictError(
                        "Gateway active snapshot 当前需要恢复，禁止继续启动: "
                        f"state={active.state}, error={active.last_error}"
                    )
                persisted_payload = active.payload
                persisted_source_baseline = active.source_baseline
        if persisted_payload is not None:
            if persisted_source_baseline is None:
                raise ConfigConflictError(
                    "Gateway active/pending snapshot 缺少来源基线"
                )
            raw_gateway_config = dict(persisted_payload)
            source_details = list(_gateway_persisted_source_details(persisted_source_baseline))
            documents = {
                str(detail["uri"]): detail["document"]
                for detail in persisted_source_baseline.values()
                if isinstance(detail, dict) and isinstance(detail.get("document"), dict)
            }
            loaded_persisted_snapshot = True
    graph = ConfigGraph(
        domain="gateway", version=6, owner_id=gateway_id or "local", entry=entry,
        aliases={"builtin:gateway": resolved_inline_config_path, "user:gateway": resolved_config_path,
                 "user:gateway_local": resolved_local_config_path},
        settings_schema={"$ref": "#/$defs/effectiveSettings", "$defs": schema["$defs"]},
        options_schema={"type": "object"}, source_uri=source_uri,
        registered_uris={source_uri(path): path for path in known},
        authorize_source=lambda path: ".boxteam" not in path.parts, read_source=read_source,
        source_documents=documents,
        document_overrides={config_file_uri(scope="gateway" if gateway_id else "user",
                                            scope_id=gateway_id or "local", logical_name=key): document
                            for key, document in (source_overrides or {}).items()},
    )
    scope = graph.resolve_scope()
    if loaded_persisted_snapshot:
        source_paths = list(graph.source_locations.values())
    if not loaded_persisted_snapshot:
        settings = cast(dict[str, object], thaw(scope.settings))
        raw_gateway_config = {**cast(dict[str, object], settings["gateway"]),
                              "network": settings["network"], "config_version": 6}
    _normalize_connection_ids(
        raw_gateway_config,
        state_store=state_store if not loaded_persisted_snapshot else None,
        persist_migrations=False,
        allow_new_ids=allow_new_connection_ids,
    )
    NetworkSettings.model_validate(raw_gateway_config["network"])
    validate_config_payload(raw_gateway_config, schema=schema)
    plugin_sources(raw_gateway_config)
    raw_workspaces = raw_gateway_config["workspaces"]
    raw_gateway_config = bound_plugin_configuration(raw_gateway_config)
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    validated_workspaces = cast(list[dict[str, object]], raw_workspaces)
    result = GatewayConfig(
        workspaces=tuple(
            workspace
            for item in validated_workspaces
            if cast(bool, item.get("enabled", True))
            if (workspace := _workspace_from_validated_config(item)).enabled
        ),
        session_catalog_refresh_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "refresh_interval_seconds",
            default=30,
        ),
        session_catalog_max_concurrency=_positive_integer_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "max_concurrency",
            default=8,
        ),
        session_catalog_request_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "request_timeout_seconds",
            default=30,
        ),
        gateway_process_health_request_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "health",
            "request_timeout_seconds",
            default=2,
        ),
        gateway_process_health_poll_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "health",
            "poll_interval_seconds",
            default=0.5,
        ),
        gateway_process_connection_drain_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "lifecycle",
            "connection_drain_timeout_seconds",
            default=2,
        ),
        default_workspace_skill_groups=_skill_groups_config(raw_gateway_config),
        local_skill_updates=SourceUpdatePolicy.model_validate(
            _nested_config_value(raw_gateway_config, "resources", "skill_sources", "local_updates") or {}
        ),
        skill_sources=validate_skill_sources(_nested_config_value(raw_gateway_config, "resources", "skill_sources", "sources") or []),
        history_loading=_history_loading_config(raw_gateway_config),
        revision=config_revision(raw_gateway_config),
        schema_path=resolved_schema_path,
        source_paths=tuple(source_paths),
        source_details=tuple(source_details),
        payload=dict(raw_gateway_config),
        graph=graph, inline_path=resolved_inline_config_path, entry_path=entry,
    )
    if not loaded_persisted_snapshot:
        for file in graph.dependencies.values():
            verify_stable_config_file(file)
        if state_store is not None and persist_migrations:
            _normalize_connection_ids(raw_gateway_config, state_store=state_store)
        for precedence, path in enumerate(dict.fromkeys(node.location.path for node in scope.chain)):
            key, layer = source_identity(path)
            file = graph.dependencies[path]
            document = cast(dict[str, object], thaw(graph.source_documents[source_uri(path)]))
            if source_payloads is not None:
                source_payloads[key] = deepcopy(document)
            record = None
            if state_store is not None and persist_migrations and layer != "inline":
                verify_stable_config_file(file)
                previous = state_store.get_source_layer(key)
                record = state_store.sync_config_source(
                    config_key=key, uri=source_uri(path), config_version=6, presence=file.presence,
                    payload=document, layer_digest=file.digest,
                    expected_layer_revision=previous.layer_revision if previous else None,
                    expected_layer_digest=previous.layer_digest if previous else None,
                    journal_origin="loader",
                )
            elif state_store is not None:
                record = state_store.get_source_layer(key)
            source_paths.append(path)
            source_details.append(ConfigSource(
                uri=source_uri(path), layer=layer, precedence=precedence, loaded=True, source_key=key,
                presence=file.presence, layer_digest=file.digest,
                layer_revision=record.layer_revision if record else None,
                source_generation=record.source_generation if record else None,
            ))
    return replace(result, source_paths=tuple(source_paths), source_details=tuple(source_details))

def _normalize_consumer_health_digests(
    digests: dict[str, str],
) -> dict[str, str]:
    if not isinstance(digests, dict):
        raise ConfigConflictError("Gateway consumer health digest 必须是对象")
    normalized: dict[str, str] = {}
    for consumer_id, digest in digests.items():
        if (
            not isinstance(consumer_id, str)
            or not consumer_id.strip()
            or not isinstance(digest, str)
            or not digest.strip()
            or re.fullmatch(r"[0-9a-f]{64}", digest.strip()) is None
        ):
            raise ConfigConflictError(
                "Gateway consumer health digest 必须包含非空 consumer_id/digest"
            )
        normalized[consumer_id.strip()] = digest.strip()
    return dict(sorted(normalized.items()))
def _consumer_health_digests_from_proof(
    health_proof: dict[str, object],
) -> dict[str, str]:
    raw_digests = health_proof.get("consumer_health_digests", {})
    if not isinstance(raw_digests, dict):
        raise ConfigConflictError(
            "Gateway health proof 的 consumer_health_digests 必须是对象"
        )
    return _normalize_consumer_health_digests(
        {str(consumer_id): digest for consumer_id, digest in raw_digests.items()}
    )
def _require_gateway_consumer_health_digests(
    digests: dict[str, str],
) -> dict[str, str]:
    normalized = _normalize_consumer_health_digests(digests)
    missing = REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS.difference(normalized)
    extra = set(normalized).difference(REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS)
    if missing or extra:
        raise ConfigConflictError(
            "Gateway health proof 必须包含完整 consumer 摘要集合: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return normalized
