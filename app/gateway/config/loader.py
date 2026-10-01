from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, cast

from app.core.config_sources import ConfigSource, config_revision
from app.core.path_utils import (
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config.source_vrn import inline_config_source_vrn
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    SecretReferenceRequiredError,
)
from configs.installer import resolve_config_resource_source
from configs.runtime import merge_json_objects, read_jsonc_object, validate_config

from .connection_ids import _normalize_connection_ids
from .sources import _gateway_source_detail, _load_or_migrate_gateway_override
from .values import (
    _GATEWAY_SOURCE_LAYER_AUTHORITY,
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
    ConfiguredTheme,
    GatewayConfig,
    _configured_theme_background,
    _history_loading_config,
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
) -> GatewayConfig:
    """加载 Gateway 内置默认、用户配置和本地覆盖。"""
    resolved_config_path = config_path or get_user_gateway_config_path()
    resolved_schema_path = schema_path or get_user_gateway_schema_path()
    resolved_inline_config_path = inline_config_path or resolve_config_resource_source(
        "gateway_inline.jsonc"
    )
    resolved_local_config_path = local_config_path or (
        get_user_gateway_local_config_path()
        if config_path is None
        else resolved_config_path.parent / "gateway_local.jsonc"
    )
    raw_gateway_config = read_jsonc_object(resolved_inline_config_path)
    source_paths: list[Path] = [resolved_inline_config_path]
    user_override: dict[str, object] | None = None
    local_override: dict[str, object] | None = None
    source_details: list[ConfigSource] = [
        ConfigSource(
            vrn=inline_config_source_vrn(logical_name="gateway_inline"),
            layer="inline",
            precedence=0,
            loaded=True,
        )
    ]
    loaded_persisted_snapshot = False
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
        persisted_source_key: str | None = None
        persisted_digest: str | None = None
        persisted_generation: int | None = None
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
            persisted_source_key = "pending_snapshot"
            persisted_digest = pending.effective_digest
            persisted_generation = None
        else:
            active = state_store.get_active_config_snapshot("gateway")
            if active is not None:
                if active.state != "active":
                    raise ConfigConflictError(
                        "Gateway active snapshot 当前需要恢复，禁止继续启动: "
                        f"state={active.state}, error={active.last_error}"
                    )
                persisted_payload = active.payload
                persisted_source_key = "active_snapshot"
                persisted_digest = active.effective_digest
                persisted_generation = active.source_generation
        if persisted_payload is not None:
            raw_gateway_config = dict(persisted_payload)
            source_paths = [resolved_inline_config_path, state_store.path]
            source_details = [
                source_details[0],
                ConfigSource(
                    vrn=None,
                    layer="sqlite",
                    precedence=1,
                    loaded=True,
                    source_key=persisted_source_key,
                    layer_digest=persisted_digest,
                    source_generation=(
                        int(persisted_generation)
                        if persisted_generation is not None
                        else None
                    ),
                ),
            ]
            loaded_persisted_snapshot = True
    if state_store is None:
        # 无 state store 分支同样只查权威表，保证与 `_gateway_source_detail` 逐字一致。
        for config_key, fallback_path in (
            ("gateway_mutable_override", resolved_config_path),
            ("gateway_local_mutable_override", resolved_local_config_path),
        ):
            layer, precedence = _GATEWAY_SOURCE_LAYER_AUTHORITY[config_key]
            source_details.append(
                ConfigSource(
                    vrn=None,
                    layer=layer,
                    precedence=precedence,
                    loaded=fallback_path.is_file(),
                    source_key=config_key,
                    presence="present" if fallback_path.is_file() else "absent",
                )
            )
        if resolved_config_path.is_file():
            raw_gateway_config = merge_json_objects(
                raw_gateway_config,
                read_jsonc_object(resolved_config_path),
            )
            source_paths.append(resolved_config_path)
        if resolved_local_config_path.is_file():
            raw_gateway_config = merge_json_objects(
                raw_gateway_config,
                read_jsonc_object(resolved_local_config_path),
            )
            source_paths.append(resolved_local_config_path)
    elif not loaded_persisted_snapshot:
        user_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_mutable_override",
            path=resolved_config_path,
            persist_migrations=persist_migrations,
        )
        local_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_local_mutable_override",
            path=resolved_local_config_path,
            persist_migrations=persist_migrations,
        )
        source_details.extend(
            [
                _gateway_source_detail(
                    state_store,
                    "gateway_mutable_override",
                    fallback_path=resolved_config_path,
                ),
                _gateway_source_detail(
                    state_store,
                    "gateway_local_mutable_override",
                    fallback_path=resolved_local_config_path,
                ),
            ]
        )
    if user_override is not None:
        raw_gateway_config = merge_json_objects(raw_gateway_config, user_override)
        if state_store.path not in source_paths:
            source_paths.append(state_store.path)
    if local_override is not None:
        raw_gateway_config = merge_json_objects(raw_gateway_config, local_override)
        if state_store.path not in source_paths:
            source_paths.append(state_store.path)
    validate_config(
        raw_gateway_config,
        config_path=resolved_config_path,
        schema_path=resolved_schema_path,
    )
    _normalize_connection_ids(
        raw_gateway_config,
        config_path=resolved_config_path,
        state_store=(
            state_store
            if persist_migrations and not loaded_persisted_snapshot
            else None
        ),
    )
    # 旧版先按 v1 结构校验，再校验补入 connection_id 的规范化候选。
    validate_config(
        raw_gateway_config,
        config_path=resolved_config_path,
        schema_path=resolved_schema_path,
    )
    raw_workspaces = raw_gateway_config["workspaces"]
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    validated_workspaces = cast(list[dict[str, object]], raw_workspaces)
    raw_ui = cast(dict[str, object], raw_gateway_config.get("ui", {}))
    raw_theme = cast(dict[str, object], raw_ui.get("theme", {}))
    raw_custom_themes = cast(
        list[dict[str, object]], raw_theme.get("custom_themes", [])
    )
    return GatewayConfig(
        workspaces=tuple(
            workspace
            for item in validated_workspaces
            if cast(bool, item.get("enabled", True))
            if (workspace := _workspace_from_validated_config(item)).enabled
        ),
        default_theme_id=cast(str, raw_theme.get("default_theme_id", "warm")),
        custom_themes=tuple(
            ConfiguredTheme(
                id=cast(str, item["id"]),
                label=cast(str, item["label"]),
                extends=cast(Literal["warm", "green", "blue"], item["extends"]),
                color_scheme=cast(
                    Literal["light", "dark"], item.get("color_scheme", "light")
                ),
                tokens=cast(dict[str, str], item.get("tokens", {})),
                background=_configured_theme_background(
                    cast(dict[str, object] | None, item.get("background")),
                    config_root=resolved_config_path.parent,
                ),
            )
            for item in raw_custom_themes
        ),
        # 这些运行时默认值来自 inline 配置；schema 校验已在上方完成，读取失败时保留
        # 旧常量作为仅针对旧自定义 inline 文件的兼容兜底。
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
        session_generator_poll_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_generators",
            "scheduler",
            "poll_interval_seconds",
            default=1,
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
        history_loading=_history_loading_config(raw_gateway_config),
        revision=config_revision(raw_gateway_config),
        schema_path=resolved_schema_path,
        source_paths=tuple(source_paths),
        source_details=tuple(source_details),
        payload=dict(raw_gateway_config),
    )
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
