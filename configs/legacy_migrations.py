"""只用于读取拆分前 v1-v4 `boxteam.jsonc` 的内容迁移。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import commentjson

from app.agents.builtin_tool_registry import builtin_tool_id_for_factory

CURRENT_CONFIG_VERSION = 4
MISSING_CONFIG_VERSION = 1

_LEGACY_GATEWAY_WORKSPACE_FIELDS = frozenset(
    {
        "remote_workspace_path",
        "remote_backend_host",
        "remote_backend_port",
        "remote_terminal_backend_host",
        "remote_terminal_backend_port",
        "remote_browser_backend_host",
        "remote_browser_backend_port",
        "remote_services",
    }
)

# TODO: 删除不再受支持的 v1 配置迁移时，一并删除这些旧工厂名映射。
_LEGACY_BUILTIN_FACTORIES = {
    "app.agents.tools.session_history:create_read_session_recent_text_messages_tool": (
        "read_context"
    ),
    "app.agents.tools.session_history:create_read_session_context_jsonl_tool": (
        "read_context"
    ),
    "app.agents.tools.session_history:create_grep_session_context_jsonl_tool": (
        "search_context"
    ),
}


@dataclass(frozen=True, slots=True)
class ConfigMigrationResult:
    config: dict[str, object]
    source_version: int
    target_version: int
    changed: bool
    source_text: str | None = None


def migrate_config(raw_config: Mapping[str, object]) -> ConfigMigrationResult:
    """将单个配置源迁移到当前版本，不修改调用方传入的数据。"""
    config = deepcopy(dict(raw_config))
    source_version = _read_version(config)
    if source_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            "配置版本高于当前程序支持范围: "
            f"config_version={source_version}, supported={CURRENT_CONFIG_VERSION}"
        )

    version = source_version
    while version < CURRENT_CONFIG_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"缺少配置迁移器: v{version} -> v{version + 1}")
        config = migration(config)
        version += 1

    return ConfigMigrationResult(
        config=config,
        source_version=source_version,
        target_version=version,
        changed=config != dict(raw_config),
    )


def read_and_migrate_config(
    path: Path,
    *,
    persist: bool,
) -> ConfigMigrationResult:
    """读取 JSONC 配置并迁移；持久化时使用并发保护的原子替换。"""
    original_text = path.read_text(encoding="utf-8")
    parsed = commentjson.loads(original_text)
    if not isinstance(parsed, dict):
        raise TypeError(f"配置文件根节点必须是对象: {path}")
    result = migrate_config(parsed)
    file_result = ConfigMigrationResult(
        config=result.config,
        source_version=result.source_version,
        target_version=result.target_version,
        changed=result.changed,
        source_text=original_text,
    )
    if persist:
        persist_config_migration(path, file_result)
    return file_result


def persist_config_migration(path: Path, result: ConfigMigrationResult) -> None:
    """在调用方完成候选校验后，原子提交此前读取的迁移结果。"""
    if not result.changed:
        return
    if result.source_text is None:
        raise ValueError("文件迁移结果缺少原始文本，无法执行并发保护写回")
    current_text = path.read_text(encoding="utf-8")
    if current_text != result.source_text:
        current_config = commentjson.loads(current_text)
        if isinstance(current_config, dict):
            concurrent_result = migrate_config(current_config)
            if (
                not concurrent_result.changed
                and concurrent_result.config == result.config
            ):
                return
        raise RuntimeError(f"迁移期间配置文件已被其他进程修改，已取消写入: {path}")
    _atomic_write_json(path, result.config)


def _read_version(config: Mapping[str, object]) -> int:
    raw_version = config.get("config_version", MISSING_CONFIG_VERSION)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise TypeError("config_version 必须是整数")
    if raw_version < 1:
        raise ValueError("config_version 必须大于或等于 1")
    return raw_version


def _migrate_v1_to_v2(config: dict[str, object]) -> dict[str, object]:
    agents = config.get("agents")
    if isinstance(agents, Mapping):
        for agent_id, raw_agent in agents.items():
            if not isinstance(raw_agent, dict):
                continue
            tools = raw_agent.get("tools")
            if not isinstance(tools, dict):
                continue
            custom = tools.get("custom")
            if not isinstance(custom, list):
                continue
            tools["custom"] = _migrate_custom_tools(
                custom,
                context=f"agents.{agent_id}.tools.custom",
            )
    config["config_version"] = 2
    return config


def _migrate_v2_to_v3(config: dict[str, object]) -> dict[str, object]:
    agents = config.get("agents")
    if isinstance(agents, Mapping):
        for raw_agent in agents.values():
            if not isinstance(raw_agent, dict):
                continue
            tools = raw_agent.get("tools")
            if not isinstance(tools, dict):
                continue
            custom = tools.get("custom")
            if not isinstance(custom, list):
                continue
            configured_tool_ids = {
                item.get("tool_id")
                for item in custom
                if isinstance(item, Mapping) and isinstance(item.get("tool_id"), str)
            }
            if "listBrowserPage" in configured_tool_ids:
                continue
            first_browser_index = next(
                (
                    index
                    for index, item in enumerate(custom)
                    if isinstance(item, Mapping)
                    and item.get("tool_id")
                    in {
                        "openBrowserPage",
                        "readPage",
                        "navigatePage",
                        "clickElement",
                        "typeInPage",
                        "hoverElement",
                        "dragElement",
                        "handleDialog",
                        "screenshotPage",
                        "runPlaywrightCode",
                    }
                ),
                None,
            )
            if first_browser_index is not None:
                custom.insert(first_browser_index, {"tool_id": "listBrowserPage"})
    config["config_version"] = 3
    return config


def _migrate_v3_to_v4(config: dict[str, object]) -> dict[str, object]:
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        workspaces = gateway.get("workspaces")
        if isinstance(workspaces, list):
            migrated_workspaces: list[object] = []
            for raw_workspace in workspaces:
                if not isinstance(raw_workspace, Mapping):
                    migrated_workspaces.append(raw_workspace)
                    continue
                workspace = dict(raw_workspace)
                uses_legacy_ssh = workspace.get("kind") == "ssh"
                uses_legacy_fields = bool(
                    _LEGACY_GATEWAY_WORKSPACE_FIELDS & workspace.keys()
                )
                if uses_legacy_ssh or uses_legacy_fields:
                    workspace["kind"] = "remote_gateway"
                    workspace.setdefault("remote_gateway_port", 8014)
                    for field in _LEGACY_GATEWAY_WORKSPACE_FIELDS:
                        workspace.pop(field, None)
                migrated_workspaces.append(workspace)
            gateway["workspaces"] = migrated_workspaces
    config["config_version"] = 4
    return config


_MIGRATIONS = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


def _migrate_custom_tools(
    raw_specs: list[object],
    *,
    context: str,
) -> list[object]:
    migrated: list[object] = []
    stable_specs: dict[str, dict[str, object]] = {}
    for index, raw_spec in enumerate(raw_specs):
        if not isinstance(raw_spec, Mapping):
            migrated.append(deepcopy(raw_spec))
            continue
        spec = deepcopy(dict(raw_spec))
        factory = spec.get("factory")
        if not isinstance(factory, str):
            migrated.append(spec)
            continue
        tool_id = builtin_tool_id_for_factory(factory)
        if tool_id is None:
            tool_id = _LEGACY_BUILTIN_FACTORIES.get(factory)
        if tool_id is None:
            migrated.append(spec)
            continue

        stable_spec = {"tool_id": tool_id}
        for field in ("options", "description"):
            if field in spec:
                stable_spec[field] = spec[field]
        previous = stable_specs.get(tool_id)
        if previous is not None:
            if previous != stable_spec:
                raise ValueError(
                    f"{context}[{index}] 迁移后与已有 {tool_id!r} 配置冲突"
                )
            continue
        stable_specs[tool_id] = stable_spec
        migrated.append(stable_spec)
    return migrated


def _atomic_write_json(path: Path, config: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"拒绝迁移符号链接配置文件: {path}")
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
