from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from app.core.config_sources import ConfigSource, ConfigSourceLayer
from app.core.history_loading import (
    DEFAULT_ANCHOR_AFTER_TURNS,
    DEFAULT_ANCHOR_BEFORE_TURNS,
    DEFAULT_ANCHOR_INCLUDE,
    DEFAULT_INITIAL_INCLUDE,
    DEFAULT_INITIAL_TURNS,
    HistoryLoadingConfig,
)
from app.core.path_utils import get_user_config_root

# Gateway 来源权威表：每个来源键的逻辑来源层与 precedence 只在本文件登记一次；`inline:0`
# 是 writer 为无 source_key 的 inline 记录生成的 canonical baseline key。
# `_gateway_source_detail`、inline 构造、无 store 分支及 baseline 恢复都只查它。Gateway 控制面库
# 与工作区库是两个独立持久化 owner（owner 裁定 B2「不合并」），故本表是 Gateway 侧
# 自有权威表，MUST NOT 从 workspace 侧 import 共享；但取值语义与 workspace 侧一致。
# `inline:0`→`inline`、`gateway_mutable_override`→`user` 与
# `gateway_local_mutable_override`→`user_local` 与无 store 分支逐字一致；有 store 时
# 共享 `gateway.sqlite` 只是**承载事实**，MUST NOT 有损改写成 carrier 名对外暴露；
# 不可寻址性一律以 `vrn=None` 表达。
_GATEWAY_SOURCE_LAYER_AUTHORITY: dict[str, tuple[ConfigSourceLayer, int]] = {
    "inline:0": ("inline", 0),
    "gateway_mutable_override": ("user", 1),
    "gateway_local_mutable_override": ("user_local", 2),
}
@dataclass(frozen=True, slots=True)
class ConfiguredRemoteGateway:
    host: str
    username: str
    private_key_path: str
    kind: Literal["remote_gateway"] = "remote_gateway"
    connection_id: str = ""
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
REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS = frozenset(
    {
        "catalog-generator-scheduler",
        "health-controller",
        "registry-batch",
        "ssh-tunnel-proxy",
        "workspace-process",
        "remote-projection",
    }
)
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
    payload: dict[str, object] = field(default_factory=dict)
GatewayConfigRuntimeRollback = Callable[[], Awaitable[None]]
GatewayConfigRuntimeApplier = Callable[
    [GatewayConfig, GatewayConfig, str, Callable[[], None]],
    Awaitable[GatewayConfigRuntimeRollback | None],
]
def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredRemoteGateway:
    return ConfiguredRemoteGateway(
        name=cast(str | None, raw.get("name")),
        host=cast(str, raw["host"]),
        port=cast(int, raw.get("port", 22)),
        ssh_config_host=cast(str | None, raw.get("ssh_config_host")),
        remote_pair_command=cast(str | None, raw.get("remote_pair_command")),
        username=cast(str, raw["username"]),
        private_key_path=cast(str, raw["private_key_path"]),
        connection_id=cast(str, raw["connection_id"]),
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
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
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
def resolve_gateway_path(value: str, *, config_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((config_root or get_user_config_root()) / raw_path).resolve()
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
