"""原配置来源中的插件参数及绑定后的有效投影；不读取安装包或保存状态。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PluginParameters = dict[str, dict[str, dict[str, JsonValue]]]


class PluginPrivateInputReference(BaseModel):
    """原配置 owner 的不可变私有文件引用，不承载原生路径或秘密。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    kind: Literal["plugin_private_file"] = "plugin_private_file"
    gateway_id: str = Field(min_length=1)
    workspace_id: str | None = None
    package_id: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    parameter_pointer: str = Field(pattern=r"^(?:/(?:[^~/]|~[01])*)+$")
    version: str = Field(pattern=r"^[a-f0-9]{32}$")


def plugin_parameters(config: Mapping[str, object]) -> PluginParameters:
    plugins = config.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise TypeError("plugins 必须是对象")
    configuration = plugins.get("configuration", {})
    if not isinstance(configuration, Mapping):
        raise TypeError("plugins.configuration 必须是对象")
    values = configuration.get("values", {})
    if not isinstance(values, dict) or any(
        not isinstance(package, dict) or any(not isinstance(value, dict) for value in package.values())
        for package in values.values()
    ):
        raise TypeError("插件参数必须按 package_id／plugin_id 保存为 JSON 对象")
    return cast(PluginParameters, values)


def bound_plugin_configuration(config: dict[str, object]) -> dict[str, object]:
    """未绑定值只留在来源；版本和 enabled 继续由原 runtime.entries 持有。"""
    values = plugin_parameters(config)
    plugins = cast(dict[str, object], config.get("plugins", {}))
    if "configuration" not in plugins:
        return config
    runtime = cast(dict[str, object], plugins.get("runtime", {}))
    entries = cast(list[dict[str, object]], runtime.get("entries", []))
    effective: PluginParameters = {}
    for entry in entries:
        package_id, plugin_id = str(entry["package_id"]), str(entry["plugin_id"])
        if plugin_id in values.get(package_id, {}):
            effective.setdefault(package_id, {})[plugin_id] = values[package_id][plugin_id]
    projected = {key: value for key, value in plugins.items() if key != "configuration"}
    if effective:
        projected["configuration"] = {"values": effective}
    return {**config, "plugins": projected}
