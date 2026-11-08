"""Workspace 配置中的服务插件绑定解析。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.core.workspace_identity import read_workspace_id
from app.schemas.gateway.service_plugin import PluginBinding


def workspace_plugin_entries(config: Mapping[str, object]) -> list[object]:
    plugins = config.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise TypeError("plugins 必须是对象")
    runtime = plugins.get("runtime", {})
    if not isinstance(runtime, Mapping):
        raise TypeError("plugins.runtime 必须是对象")
    entries = runtime.get("entries", [])
    if not isinstance(entries, list):
        raise TypeError("plugins.runtime.entries 必须是数组")

    return entries


def workspace_plugin_bindings(
    config: Mapping[str, object], *, workspace_id: str | None,
) -> list[PluginBinding]:
    entries = workspace_plugin_entries(config)
    if entries and not workspace_id:
        raise ValueError("配置 Workspace 插件绑定时必须有真实 workspace_id")

    bindings: list[PluginBinding] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError("Workspace 插件绑定条目必须是对象")
        if "workspace_id" in entry:
            raise ValueError("Workspace 插件绑定不得自行填写 workspace_id")
        binding = PluginBinding.model_validate({**entry, "workspace_id": workspace_id})
        key = (binding.package_id, binding.plugin_id)
        if key in seen:
            raise ValueError("同一 Workspace 插件绑定不得重复")
        seen.add(key)
        bindings.append(binding)
    return bindings


def workspace_plugin_bindings_for_root(
    config: Mapping[str, object], *, workspace_root: Path | None,
) -> list[PluginBinding]:
    entries = workspace_plugin_entries(config)
    workspace_id = (
        read_workspace_id(workspace_root)
        if entries and workspace_root is not None
        else None
    )
    return workspace_plugin_bindings(config, workspace_id=workspace_id)
