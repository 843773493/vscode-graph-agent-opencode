from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from app.core.config_sources import ConfigSource, config_revision
from app.core.history_loading import (
    DEFAULT_ANCHOR_AFTER_TURNS,
    DEFAULT_ANCHOR_BEFORE_TURNS,
    DEFAULT_ANCHOR_INCLUDE,
    DEFAULT_INITIAL_INCLUDE,
    DEFAULT_INITIAL_TURNS,
    HistoryLoadingConfig,
)
from app.core.path_utils import (
    get_user_config_root,
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from configs.installer import resolve_config_resource_source
from configs.runtime import merge_json_objects, read_jsonc_object, validate_config


@dataclass(frozen=True, slots=True)
class ConfiguredRemoteGateway:
    host: str
    username: str
    private_key_path: str
    kind: Literal["remote_gateway"] = "remote_gateway"
    name: str | None = None
    port: int = 22
    ssh_config_host: str | None = None
    remote_pair_command: str | None = None
    remote_gateway_port: int = 8014
    activate: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ConfiguredTheme:
    id: str
    label: str
    extends: Literal["warm", "green", "blue"]
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background: dict[str, object] | None = None


GatewayHistoryLoadingConfig = HistoryLoadingConfig


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    workspaces: tuple[ConfiguredRemoteGateway, ...] = ()
    default_theme_id: str = "warm"
    custom_themes: tuple[ConfiguredTheme, ...] = ()
    session_catalog_refresh_interval_seconds: float = 30
    session_catalog_max_concurrency: int = 8
    session_catalog_request_timeout_seconds: float = 30
    session_generator_poll_interval_seconds: float = 1
    gateway_process_health_request_timeout_seconds: float = 2
    gateway_process_health_poll_interval_seconds: float = 0.5
    gateway_process_connection_drain_timeout_seconds: float = 2
    default_workspace_skill_groups: tuple[str, ...] = ()
    history_loading: GatewayHistoryLoadingConfig = field(
        default_factory=GatewayHistoryLoadingConfig,
    )
    revision: str = ""
    schema_path: Path | None = None
    source_paths: tuple[Path, ...] = ()
    source_details: tuple[ConfigSource, ...] = ()


def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredRemoteGateway:
    return ConfiguredRemoteGateway(
        name=cast(str | None, raw.get("name")),
        host=cast(str, raw["host"]),
        port=cast(int, raw.get("port", 22)),
        ssh_config_host=cast(str | None, raw.get("ssh_config_host")),
        remote_pair_command=cast(str | None, raw.get("remote_pair_command")),
        username=cast(str, raw["username"]),
        private_key_path=cast(str, raw["private_key_path"]),
        remote_gateway_port=cast(int, raw.get("remote_gateway_port", 8014)),
        activate=cast(bool, raw.get("activate", False)),
        enabled=cast(bool, raw.get("enabled", True)),
    )


def _nested_config_value(raw: dict[str, object], *keys: str) -> object | None:
    current: object = raw
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _positive_number_config(
    raw: dict[str, object],
    *keys: str,
    default: float,
) -> float:
    value = _nested_config_value(raw, *keys)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def _positive_integer_config(
    raw: dict[str, object],
    *keys: str,
    default: int,
) -> int:
    value = _nested_config_value(raw, *keys)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _skill_groups_config(raw: dict[str, object]) -> tuple[str, ...]:
    value = _nested_config_value(raw, "runtime", "workspace", "default_skill_groups")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError("runtime.workspace.default_skill_groups 必须是字符串数组")
    return tuple(value)


def _history_loading_config(raw: dict[str, object]) -> GatewayHistoryLoadingConfig:
    initial_value = _nested_config_value(
        raw,
        "features",
        "session_history",
        "loading",
        "progressive",
        "initial",
    )
    anchor_value = _nested_config_value(
        raw,
        "features",
        "session_history",
        "loading",
        "progressive",
        "anchor",
    )
    initial = cast(dict[str, object], initial_value or {})
    anchor = cast(dict[str, object], anchor_value or {})
    initial_turns = initial.get("turns", DEFAULT_INITIAL_TURNS)
    anchor_before_turns = anchor.get("before_turns", DEFAULT_ANCHOR_BEFORE_TURNS)
    anchor_after_turns = anchor.get("after_turns", DEFAULT_ANCHOR_AFTER_TURNS)
    initial_include = initial.get(
        "include",
        list(DEFAULT_INITIAL_INCLUDE),
    )
    anchor_include = anchor.get("include", list(DEFAULT_ANCHOR_INCLUDE))
    if (
        isinstance(initial_turns, bool)
        or not isinstance(initial_turns, int)
        or initial_turns < 1
        or isinstance(anchor_before_turns, bool)
        or not isinstance(anchor_before_turns, int)
        or anchor_before_turns < 1
        or isinstance(anchor_after_turns, bool)
        or not isinstance(anchor_after_turns, int)
        or anchor_after_turns < 1
        or not isinstance(initial_include, list)
        or not all(isinstance(item, str) for item in initial_include)
        or not isinstance(anchor_include, list)
        or not all(isinstance(item, str) for item in anchor_include)
    ):
        raise TypeError("Gateway 历史加载配置结构非法")
    return GatewayHistoryLoadingConfig(
        initial_turns=initial_turns,
        initial_include=tuple(initial_include),
        anchor_before_turns=anchor_before_turns,
        anchor_after_turns=anchor_after_turns,
        anchor_include=tuple(anchor_include),
    )


def load_gateway_config(
    *,
    config_path: Path | None = None,
    schema_path: Path | None = None,
    local_config_path: Path | None = None,
    inline_config_path: Path | None = None,
    state_store: GatewayStateStore | None = None,
) -> GatewayConfig:
    """加载 Gateway 内置默认、用户配置和本地覆盖。"""
    resolved_config_path = config_path or get_user_gateway_config_path()
    resolved_schema_path = schema_path or get_user_gateway_schema_path()
    resolved_inline_config_path = inline_config_path or resolve_config_resource_source(
        "gateway_inline.jsonc"
    )
    resolved_local_config_path = (
        local_config_path
        or (
            get_user_gateway_local_config_path()
            if config_path is None
            else resolved_config_path.parent / "gateway_local.jsonc"
        )
    )
    raw_gateway_config = read_jsonc_object(resolved_inline_config_path)
    source_paths: list[Path] = [resolved_inline_config_path]
    user_override: dict[str, object] | None = None
    local_override: dict[str, object] | None = None
    source_details: list[ConfigSource] = [
        ConfigSource(
            path=resolved_inline_config_path,
            layer="inline",
            precedence=0,
            loaded=True,
        )
    ]
    if state_store is None:
        source_details.extend(
            [
                ConfigSource(
                    path=resolved_config_path,
                    layer="user",
                    precedence=1,
                    loaded=resolved_config_path.is_file(),
                ),
                ConfigSource(
                    path=resolved_local_config_path,
                    layer="user_local",
                    precedence=2,
                    loaded=resolved_local_config_path.is_file(),
                ),
            ]
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
    else:
        user_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_mutable_override",
            path=resolved_config_path,
        )
        local_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_local_mutable_override",
            path=resolved_local_config_path,
        )
        source_details.extend(
            [
                ConfigSource(
                    path=state_store.path,
                    layer="sqlite",
                    precedence=1,
                    loaded=user_override is not None,
                ),
                ConfigSource(
                    path=state_store.path,
                    layer="sqlite",
                    precedence=2,
                    loaded=local_override is not None,
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
    )


def _load_or_migrate_gateway_override(
    *,
    state_store: GatewayStateStore,
    config_key: str,
    path: Path,
) -> dict[str, object] | None:
    record = state_store.get_config(config_key)
    if record is not None:
        return record.payload
    if not path.is_file():
        return None
    payload = read_jsonc_object(path)
    backup_path = path.with_name(f"{path.name}.migrated.bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    state_store.set_config(
        config_key=config_key,
        config_version=int(payload.get("config_version", 1)),
        payload=payload,
    )
    return payload


def _configured_theme_background(
    background: dict[str, object] | None,
    *,
    config_root: Path,
) -> dict[str, object] | None:
    if background is None or background.get("type") != "local_file":
        return background
    return {
        **background,
        "path": str(
            resolve_gateway_path(cast(str, background["path"]), config_root=config_root)
        ),
    }


def resolve_gateway_path(value: str, *, config_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((config_root or get_user_config_root()) / raw_path).resolve()
