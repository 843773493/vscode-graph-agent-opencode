"""原配置来源中的插件参数及绑定后的有效投影；不读取安装包或保存状态。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import cast

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from pydantic import JsonValue

PluginParameters = dict[str, dict[str, dict[str, JsonValue]]]


def _private_parameter(
    _validator: Validator, write_only: bool, _instance: object, _schema: Mapping[str, object],
) -> Iterator[ValidationError]:
    if write_only:
        # TODO：秘密参数接入原配置 owner 的私有引用交付；普通参数通道不得接收真实秘密。
        yield ValidationError("插件秘密参数尚未接入私有输入 owner，不能通过普通参数提交")


PluginParameterValidator = validators.extend(Draft202012Validator, {"writeOnly": _private_parameter})


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
